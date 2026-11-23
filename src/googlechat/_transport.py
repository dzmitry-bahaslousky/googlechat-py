"""Async HTTP layer: token bootstrap, binary-protobuf POSTs to /api/<method>,
and the web UI's JSON batchexecute calls."""

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx
from google.protobuf.message import Message as ProtoMessage

from ._proto import googlechat_pb2 as gc
from .auth import parse_tokens
from .exceptions import APIError, GoogleChatError, NetworkError, SessionExpiredError

logger = logging.getLogger(__name__)

BASE_URL = "https://chat.google.com"

_BROWSER_HEADERS = {
    "Origin": "https://chat.google.com",
    "Referer": "https://chat.google.com/",
    "X-Goog-Authuser": "0",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
}

_PROTO_HEADERS = {
    "Content-Type": "application/x-protobuf",
    "Accept": "application/x-protobuf",
    "X-Goog-Encode-Response-If-Executable": "base64",
}

# Browser handshake string the Gmail-embedded Chat client sends to /mole/world.
_MOLE_HS = (
    '["h_hs",null,null,[1,0],null,null,"gmail.pinto-server_20230730.06_p0",1,null,'
    '[15,38,36,35,26,30,41,18,24,11,21,14,6],null,null,"3Mu86PSulM4.en..es5",0,null,null,[0]]'
)
_MOLE_PARAMS = {
    "origin": "https://mail.google.com", "shell": "9", "hl": "en",
    "wfi": "gtn-roster-iframe-id", "hs": _MOLE_HS,
}

R = TypeVar("R", bound=ProtoMessage)

# Calls that only read. Only these are retried: retrying a write after a lost
# reply could post or change something twice.
READ_METHODS = frozenset({
    "paginated_world", "list_topics", "list_messages", "search_messages_v2",
    "get_members", "list_members", "get_user_status",
})
# Sends carry a client-made local_id; Google refuses a repeat of the same id
# with 409 instead of posting twice (verified live 2026-11-20). So these may be
# retried, and a 409 on a retry means the earlier attempt was posted.
DEDUPED_WRITES = frozenset({"create_topic", "create_message"})
# batchexecute RPC ids that only read; only these may be called (and retried).
# TBt4vb: a chat's shared files, links or media (the web UI's "Shared" panel).
READ_RPCS = frozenset({"TBt4vb"})
_BATCH_PATH = "/_/DynamiteWebUi/data/batchexecute"
RETRY_STATUSES = frozenset({429, 502, 503, 504})
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT = 30.0  # seconds; a longer Retry-After is capped
# Cookies that apply to chat.google.com. Others (e.g. accounts.google.com, set
# during a redirect) are not saved: the cookie file stores names only, and on
# reload every cookie is sent to all of .google.com.
_CHAT_COOKIE_DOMAINS = frozenset({".google.com", "google.com", "chat.google.com",
                                  ".chat.google.com"})


class AlreadyPosted(Exception):
    """A retried send got 409: an earlier attempt posted it but lost its reply."""

# The capability fields the Chat web client declares, each FULLY_SUPPORTED (2).
# Declaring a list switches off every field it leaves out, so send them all.
# List from Parley (github.com/cuza/Parley, DynamiteClient.swift, MIT).
_WEB_CAPABILITIES = (
    5, 6, 8, 9, 10, 11, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 32, 33, 37, 38,
    43, 44, 45, 46, 48, 51, 53, 54, 55, 56, 58, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 72, 73,
)


def _varint(n: int) -> bytes:
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


# Most of these fields are not in our schema, so build the wire form directly.
_WEB_CAPABILITY_BYTES = b"".join(_varint(f << 3) + b"\x02" for f in _WEB_CAPABILITIES)


def build_request_header() -> gc.RequestHeader:
    """The header the Chat web client sends.

    As the web client, Google fills naming data (e.g. member ids for unnamed
    chats) that it leaves out for the iOS client type. Verified live 2026-11-20.
    """
    h = gc.RequestHeader()
    h.client_type = gc.RequestHeader.WEB
    h.trace_id = 0
    h.client_feature_capabilities.ParseFromString(_WEB_CAPABILITY_BYTES)
    return h


def _retry_wait(response: httpx.Response | None, attempt: int) -> float:
    """Seconds to wait: the server's Retry-After if given, else 1, 2, 4..."""
    if response is not None:
        try:
            wait = float(response.headers["Retry-After"])
        except (KeyError, ValueError):
            pass
        else:
            if math.isfinite(wait):  # "nan" and "inf" parse as floats too
                return min(max(wait, 0.0), MAX_RETRY_WAIT)
    return float(2 ** (attempt - 1))


def _batch_reply(rpc_id: str, body: str) -> Any:
    """The decoded result of `rpc_id` in a batchexecute (rt=c) reply.

    The body is ")]}'" then length-prefixed chunks, one JSON array per line. The
    result line is ["wrb.fr", rpc_id, "<result as JSON text>", ...]; when the
    call failed the result is null and item 5 holds a status like [3].
    """
    try:
        for line in body.splitlines():
            if not line.startswith("["):
                continue
            for entry in json.loads(line):
                if isinstance(entry, list) and entry[:2] == ["wrb.fr", rpc_id]:
                    if entry[2] is None:
                        status = entry[5] if len(entry) > 5 else None
                        raise APIError(400, f"{rpc_id} rejected the request (status {status})")
                    return json.loads(entry[2])
    except (ValueError, TypeError, IndexError) as e:  # ValueError covers JSONDecodeError
        raise GoogleChatError(
            f"{rpc_id}: unreadable batchexecute reply; Google changed it? ({type(e).__name__})"
        ) from None
    raise GoogleChatError(f"{rpc_id}: no result in the batchexecute reply; Google changed it?")


def _network_error(method: str, e: httpx.TransportError, retried: bool) -> NetworkError:
    reason = f"{method}: network error ({type(e).__name__})"
    if retried:
        return NetworkError(f"{reason}; gave up after {MAX_ATTEMPTS} attempts.")
    # Edits, deletes and reactions are sent once: the request may have arrived.
    return NetworkError(f"{reason}; the change may or may not have been applied.")


class Transport:
    def __init__(self, cookies: dict[str, str], *, http: httpx.AsyncClient | None = None,
                 on_cookies_changed: Callable[[dict[str, str]], None] | None = None):
        self._http = http or httpx.AsyncClient(timeout=30, follow_redirects=True)
        self._http.headers.update(_BROWSER_HEADERS)
        for name, value in cookies.items():
            self._http.cookies.set(name, value, domain=".google.com")
        self._cookie_values = dict(cookies)
        self._on_cookies_changed = on_cookies_changed
        self._xsrf: str | None = None
        self._at: str | None = None  # batchexecute token
        self._tokens_lock = asyncio.Lock()
        self._sleep = asyncio.sleep  # replaced in tests

    async def aclose(self) -> None:
        await self._http.aclose()

    def _sync_cookies(self) -> None:
        """Google replaces some cookies (SIDCC, ...) on every response. Pass new
        values to the callback so they can be saved, as a browser would."""
        changed = {}
        for c in self._http.cookies.jar:
            if c.domain not in _CHAT_COOKIE_DOMAINS:
                continue
            if c.value is not None and self._cookie_values.get(c.name) != c.value:
                changed[c.name] = c.value
        if not changed:
            return
        self._cookie_values.update(changed)
        if self._on_cookies_changed:
            try:
                self._on_cookies_changed(changed)
            # Saving is best-effort: the call itself succeeded, and raising here
            # would make a send that was posted look failed (and get resent).
            except Exception as e:
                logger.warning("could not save updated cookies: %s", type(e).__name__)

    async def _tokens(self, stale: str | None = None) -> tuple[str, str | None]:
        """(XSRF token, batchexecute token): fetched on first use, and again if
        either is still `stale`. Both come from the same page.

        Concurrent callers holding the same stale token share one refetch. A
        signed-out session shows up here as SessionExpiredError.
        """
        async with self._tokens_lock:
            if self._xsrf is None or (stale is not None and stale in (self._xsrf, self._at)):
                r = await self._http.get(
                    f"{BASE_URL}/mole/world",
                    params=_MOLE_PARAMS,
                    headers={"Referer": "https://mail.google.com/"},
                )
                self._sync_cookies()
                if r.status_code in (401, 403):
                    raise SessionExpiredError("Session rejected; run `gchat login`.")
                if r.is_error:
                    raise APIError(r.status_code, "could not load /mole/world for the XSRF token")
                self._xsrf, self._at = parse_tokens(r.text)
            return self._xsrf, self._at

    async def connect(self) -> None:
        """Fetch the request tokens now, so a dead session fails here, not mid-task."""
        try:
            await self._tokens()
        except httpx.TransportError as e:
            raise NetworkError(f"could not reach chat.google.com ({type(e).__name__})") from e

    async def call(self, method: str, request: ProtoMessage, response_cls: type[R]) -> R:
        """POST `request` to /api/<method> and decode the reply as `response_cls`."""
        response = response_cls()
        response.ParseFromString(await self.post(method, request))
        return response

    async def post(self, method: str, request: ProtoMessage) -> bytes:
        """POST `request` to /api/<method> and return the raw reply body.

        Reads and sends are retried on 429/502/503/504 and network errors,
        honouring Retry-After. Other writes (edit, delete, reactions, ...) are
        never retried. A send whose retry gets 409 was already posted by an
        earlier attempt: that raises AlreadyPosted.

        A 400 is often a stale XSRF token. Google refuses such a request without
        running it, so any method is sent once more with a fresh token; a second
        400 is a genuinely bad request.
        """
        r = await self._send(method, lambda: self._post_once(method, request),
                             retryable=method in READ_METHODS or method in DEDUPED_WRITES)
        return self._check(method, r)

    async def batch_execute(self, rpc_id: str, args: list) -> Any:
        """Call one batchexecute RPC (the JSON-array endpoint the web UI uses for
        some panels) with `args`, and return its decoded result.

        Only read-only RPCs (READ_RPCS) are allowed, so all are retried like reads.
        """
        if rpc_id not in READ_RPCS:
            raise ValueError(f"{rpc_id} is not a known read-only batchexecute RPC")

        async def once() -> tuple[httpx.Response, str]:
            _, at = await self._tokens()
            if at is None:
                raise GoogleChatError("batchexecute token (SNlM0e) missing from WIZ_global_data.")
            freq = json.dumps([[[rpc_id, json.dumps(args), None, "generic"]]])
            r = await self._http.post(f"{BASE_URL}{_BATCH_PATH}",
                                      params={"rpcids": rpc_id, "rt": "c"},
                                      data={"f.req": freq, "at": at})
            self._sync_cookies()
            return r, at

        r = await self._send(rpc_id, once, retryable=True)
        return _batch_reply(rpc_id, self._check(rpc_id, r).decode())

    async def _send(self, method: str, once: Callable[[], Awaitable[tuple[httpx.Response, str]]],
                    *, retryable: bool) -> httpx.Response:
        """Run `once` (one request; returns the reply and the token it used) with
        the retry and fresh-token rules described in `post`."""
        attempts = MAX_ATTEMPTS if retryable else 1
        attempt = 1
        refreshed = False
        while True:
            try:
                r, token = await once()
            except httpx.TransportError as e:
                if attempt == attempts:
                    raise _network_error(method, e, retryable) from e
                await self._sleep(_retry_wait(None, attempt))
                attempt += 1
                continue
            if r.status_code == 400 and not refreshed:
                refreshed = True
                await self._tokens(stale=token)
                continue
            if r.status_code in RETRY_STATUSES and attempt < attempts:
                await self._sleep(_retry_wait(r, attempt))
                attempt += 1
                continue
            if r.status_code == 409 and attempt > 1 and method in DEDUPED_WRITES:
                logger.info("%s: already posted by an earlier attempt", method)
                raise AlreadyPosted(method)
            return r

    async def _post_once(self, method: str, request: ProtoMessage) -> tuple[httpx.Response, str]:
        """One POST; returns the reply and the XSRF token it was sent with."""
        request.request_header.CopyFrom(build_request_header())
        token, _ = await self._tokens()
        r = await self._http.post(
            f"{BASE_URL}/api/{method}",
            params={"rt": "b", "alt": "proto"},
            content=request.SerializeToString(),
            headers={**_PROTO_HEADERS, "X-Framework-XSRF-Token": token},
        )
        self._sync_cookies()
        return r, token

    def _check(self, method: str, r: httpx.Response) -> bytes:
        if r.status_code == 401:  # cookies rejected
            self._xsrf = None
            raise SessionExpiredError(f"{method}: session rejected; run `gchat login`.")
        if r.status_code == 400:  # already resent once with a fresh token
            raise APIError(400, f"{method} rejected the request: a malformed id or argument?")
        if r.is_error:
            raise APIError(r.status_code, f"{method} failed")
        return r.content
