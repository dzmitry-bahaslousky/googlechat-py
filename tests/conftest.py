"""Offline test harness: a fake chat.google.com behind httpx.MockTransport."""

import json
from collections.abc import Callable

import httpx
import pytest

from googlechat import GoogleChatClient
from googlechat._proto import googlechat_pb2 as gc

COOKIES = {"SID": "sid", "SAPISID": "sapisid", "__Secure-1PSID": "psid"}

REQUEST_TYPES = {
    "paginated_world": gc.PaginatedWorldRequest,
    "list_topics": gc.ListTopicsRequest,
    "list_messages": gc.ListMessagesRequest,
    "search_messages_v2": gc.SearchMessagesV2Request,
    "get_members": gc.GetMembersRequest,
    "list_members": gc.ListMembersRequest,
    "create_topic": gc.CreateTopicRequest,
    "create_message": gc.CreateMessageRequest,
    "edit_message": gc.EditMessageRequest,
    "delete_message": gc.DeleteMessageRequest,
    "create_dm_extended": gc.CreateDmExtendedRequest,
    "update_reaction": gc.UpdateReactionRequest,
    "set_typing_state": gc.SetTypingStateRequest,
    "mark_group_readstate": gc.MarkGroupReadstateRequest,
    "get_user_status": gc.GetUserStatusRequest,
}


def default_world() -> gc.PaginatedWorldResponse:
    """The chat list the tests' ids come from: spaces S1, S2, S7-S9 and DMs D1, D9."""
    world = gc.PaginatedWorldResponse()
    for i, sid in enumerate(["S1", "S2", "S7", "S8", "S9"]):
        world.world_items.add(sort_timestamp=10 - i).group_id.space_id.space_id = sid
    for did in ["D1", "D9"]:
        world.world_items.add(sort_timestamp=1).group_id.dm_id.dm_id = did
    return world


def mole_page(wiz: dict) -> str:
    return f"<script>window.WIZ_global_data = {json.dumps(wiz)};</script>"


class FakeChat:
    """Records every API call and answers from `replies` (method -> proto or callable)."""

    def __init__(self):
        self.replies: dict[str, object] = {"paginated_world": default_world()}
        self.calls: list[tuple[str, object]] = []  # (method, decoded request)
        self.http_requests: list[httpx.Request] = []
        self.status: dict[str, int] = {}  # method -> forced HTTP status
        self.status_queue: dict[str, list] = {}  # method -> [(status, headers), ...] served first
        self.set_cookies: list[str] = []  # Set-Cookie headers added to every reply
        self.mole_fetches = 0
        self.xsrf = "token-1"
        self.signed_out = False  # /mole/world serves the sign-in page
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def requests(self, method: str) -> list:
        return [req for m, req in self.calls if m == method]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.http_requests.append(request)
        path = request.url.path
        extra = [("set-cookie", c) for c in self.set_cookies]
        if path == "/mole/world":
            self.mole_fetches += 1
            wiz = {"qwAQke": "AccountsSignInUi"} if self.signed_out else {"SMqcke": self.xsrf}
            return httpx.Response(200, text=mole_page(wiz), headers=extra)
        method = path.removeprefix("/api/")
        decoded = REQUEST_TYPES[method]()
        decoded.ParseFromString(request.content)
        self.calls.append((method, decoded))
        if self.status_queue.get(method):
            status, headers = self.status_queue[method].pop(0)
            return httpx.Response(status, headers=headers)
        if method in self.status:
            return httpx.Response(self.status[method])
        reply = self.replies.get(method)
        if isinstance(reply, Callable):
            reply = reply(decoded)
        return httpx.Response(200, content=reply.SerializeToString() if reply else b"",
                              headers=extra)


@pytest.fixture
def fake() -> FakeChat:
    return FakeChat()


def fake_http(fake: FakeChat) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))


def make_client(fake: FakeChat, **kwargs) -> GoogleChatClient:
    """A client wired to `fake`; retries record their waits instead of sleeping."""
    c = GoogleChatClient(COOKIES, http=fake_http(fake), **kwargs)
    c._transport._sleep = fake.sleep
    return c


@pytest.fixture
async def client(fake: FakeChat):
    async with make_client(fake) as c:
        yield c
