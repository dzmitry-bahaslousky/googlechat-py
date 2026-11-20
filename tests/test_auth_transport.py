import json
import os

import httpx
import pytest
from google.protobuf.unknown_fields import UnknownFieldSet

from googlechat import (
    APIError, GoogleChatClient, GoogleChatError, NetworkError, NotSignedInError,
    SessionExpiredError,
)
from googlechat._proto import googlechat_pb2 as gc
from googlechat._messages import _local_id
from googlechat.auth import load_cookies, parse_xsrf_token, save_cookie_updates

from .conftest import COOKIES, fake_http, mole_page


# ── Cookie file ───────────────────────────────────────────────────────────────

def test_load_cookies_list_format(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps([{"name": k, "value": v} for k, v in COOKIES.items()]
                            + [{"key": "HSID", "value": "h"}]))
    assert load_cookies(p) == {**COOKIES, "HSID": "h"}


def test_load_cookies_dict_format(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(COOKIES))
    assert load_cookies(p) == COOKIES


def test_load_cookies_missing_file(tmp_path):
    with pytest.raises(NotSignedInError, match="no cookies"):
        load_cookies(tmp_path / "nope.json")


def test_load_cookies_incomplete(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"SID": "x"}))
    with pytest.raises(NotSignedInError, match="SAPISID"):
        load_cookies(p)


def test_from_storage_uses_cookie_file(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(COOKIES))
    assert isinstance(GoogleChatClient.from_storage(p), GoogleChatClient)


# ── XSRF page ─────────────────────────────────────────────────────────────────

def test_parse_xsrf_token():
    assert parse_xsrf_token(mole_page({"SMqcke": "abc"})) == "abc"


def test_parse_xsrf_signed_out():
    with pytest.raises(SessionExpiredError):
        parse_xsrf_token(mole_page({"qwAQke": "AccountsSignInUi"}))


def test_parse_xsrf_no_wiz_data():
    with pytest.raises(SessionExpiredError):
        parse_xsrf_token("<html>nothing</html>")


# ── Transport ─────────────────────────────────────────────────────────────────

async def test_call_sends_xsrf_cookies_and_proto(client, fake):
    fake.replies["paginated_world"] = gc.PaginatedWorldResponse()
    await client.spaces.list()

    api = fake.http_requests[-1]
    assert api.url.path == "/api/paginated_world"
    assert api.url.params["rt"] == "b" and api.url.params["alt"] == "proto"
    assert api.headers["X-Framework-XSRF-Token"] == "token-1"
    assert api.headers["Content-Type"] == "application/x-protobuf"
    assert "SID=sid" in api.headers["Cookie"]
    # Every request carries the web client's RequestHeader, with its capability list.
    header = fake.requests("paginated_world")[0].request_header
    assert header.client_type == gc.RequestHeader.WEB
    caps = header.client_feature_capabilities
    assert len(caps.ListFields()) + len(UnknownFieldSet(caps)) == 48  # all web capabilities


async def test_xsrf_fetched_once(client, fake):
    fake.replies["paginated_world"] = gc.PaginatedWorldResponse()
    await client.spaces.list()
    await client.spaces.list()
    assert fake.mole_fetches == 1


async def test_401_is_session_expired_and_next_call_refetches(client, fake):
    fake.status["paginated_world"] = 401
    with pytest.raises(SessionExpiredError):
        await client.spaces.list()

    del fake.status["paginated_world"]
    fake.xsrf = "token-2"
    fake.replies["paginated_world"] = gc.PaginatedWorldResponse()
    await client.spaces.list()
    assert fake.mole_fetches == 2
    assert fake.http_requests[-1].headers["X-Framework-XSRF-Token"] == "token-2"


async def test_400_resends_once_with_a_fresh_xsrf_token(client, fake):
    await client.connect()
    fake.xsrf = "token-2"  # the cached token-1 went stale
    fake.status_queue["edit_message"] = [(400, {})]  # any method, writes too
    await client.messages.edit("S1", "T1", "M1", "x", is_dm=False)
    first, second = [r for r in fake.http_requests if r.url.path == "/api/edit_message"]
    assert first.headers["X-Framework-XSRF-Token"] == "token-1"
    assert second.headers["X-Framework-XSRF-Token"] == "token-2"
    assert fake.mole_fetches == 2 and fake.slept == []


async def test_second_400_is_a_bad_request(client, fake):
    fake.status["edit_message"] = 400
    with pytest.raises(APIError) as e:
        await client.messages.edit("S1", "T1", "M1", "x", is_dm=False)
    assert e.value.status_code == 400 and not isinstance(e.value, SessionExpiredError)
    assert len(fake.requests("edit_message")) == 2


async def test_400_with_signed_out_session_is_session_expired(client, fake):
    await client.connect()
    fake.signed_out = True
    fake.status["paginated_world"] = 400
    with pytest.raises(SessionExpiredError):
        await client.spaces.list()
    assert len(fake.requests("paginated_world")) == 1


async def test_server_error_raises_api_error(client, fake):
    fake.status["paginated_world"] = 500
    with pytest.raises(APIError) as e:
        await client.spaces.list()
    assert e.value.status_code == 500


# ── Retries ───────────────────────────────────────────────────────────────────

async def test_reads_retry_on_429_honouring_retry_after(client, fake):
    fake.status_queue["paginated_world"] = [(429, {"Retry-After": "7"}), (503, {})]
    fake.replies["paginated_world"] = gc.PaginatedWorldResponse()
    await client.spaces.list()
    assert len(fake.requests("paginated_world")) == 3
    assert fake.slept == [7.0, 2.0]  # server's wait, then backoff for attempt 2


async def test_reads_give_up_after_three_attempts(client, fake):
    fake.status_queue["paginated_world"] = [(503, {})] * 3
    with pytest.raises(APIError) as e:
        await client.spaces.list()
    assert e.value.status_code == 503 and len(fake.requests("paginated_world")) == 3


async def test_retry_after_is_capped(client, fake):
    fake.status_queue["paginated_world"] = [(429, {"Retry-After": "3600"})]
    fake.replies["paginated_world"] = gc.PaginatedWorldResponse()
    await client.spaces.list()
    assert fake.slept == [30.0]


async def test_edits_and_reactions_are_never_retried(client, fake):
    fake.status_queue["edit_message"] = [(503, {})]
    fake.status_queue["update_reaction"] = [(503, {})]
    with pytest.raises(APIError):
        await client.messages.edit("S1", "T1", "M1", "x")
    with pytest.raises(APIError):
        await client.reactions.add("S1", "T1", "M1", "👍")
    assert len(fake.requests("edit_message")) == 1 and len(fake.requests("update_reaction")) == 1
    assert fake.slept == []


async def test_send_retries_with_the_same_local_id(client, fake):
    fake.status_queue["create_topic"] = [(503, {})]
    resp = gc.CreateTopicResponse()
    resp.topic.id.topic_id = "T5"
    resp.topic.replies.add().id.message_id = "M5"
    fake.replies["create_topic"] = resp

    sent = await client.messages.send("S1", "hi")

    first, second = fake.requests("create_topic")
    assert first.local_id == second.local_id  # the same id, so Google can't post twice
    assert (sent.id, sent.thread_id) == ("M5", "T5")


async def test_send_retry_409_means_already_posted(client, fake):
    fake.status_queue["create_message"] = [(503, {}), (409, {})]
    sent = await client.messages.reply("S1", "T1", "hi")
    assert (sent.id, sent.thread_id, sent.already_posted) == (None, "T1", True)
    assert len(fake.requests("create_message")) == 2


async def test_send_409_on_first_attempt_is_an_error(client, fake):
    fake.status_queue["create_topic"] = [(409, {})]
    with pytest.raises(APIError) as e:
        await client.messages.send("S1", "hi")
    assert e.value.status_code == 409


def test_local_ids_are_unique_within_a_millisecond():
    assert len({_local_id() for _ in range(1000)}) == 1000


async def test_reads_retry_network_errors(client, fake):
    calls = {"n": 0}

    def flaky(request):
        if request.url.path.startswith("/api/") and calls["n"] == 0:
            calls["n"] += 1
            raise httpx.ConnectError("boom")
        return fake.handler(request)
    client._transport._http._transport = httpx.MockTransport(flaky)
    fake.replies["paginated_world"] = gc.PaginatedWorldResponse()
    await client.spaces.list()
    assert fake.slept == [1.0]


# ── Rotated cookies ───────────────────────────────────────────────────────────

async def test_rotated_cookies_are_reported_once(fake):
    seen = []
    http = fake_http(fake)
    fake.set_cookies = ["SIDCC=new1; Domain=.google.com; Path=/; Secure"]
    fake.replies["paginated_world"] = gc.PaginatedWorldResponse()
    async with GoogleChatClient({**COOKIES, "SIDCC": "old"}, http=http,
                                on_cookies_changed=seen.append) as c:
        await c.spaces.list()
        await c.spaces.list()
    assert seen == [{"SIDCC": "new1"}]  # reported when it changed, not on every reply


def test_save_cookie_updates_keeps_format_and_privacy(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps([{"name": "SID", "value": "a", "domain": ".google.com"},
                             {"name": "SIDCC", "value": "old", "domain": ".google.com"}]))
    save_cookie_updates(p, {"SIDCC": "new", "OSID": "x"})
    saved = json.loads(p.read_text())
    assert [(c["name"], c["value"]) for c in saved] == [("SID", "a"), ("SIDCC", "new"), ("OSID", "x")]
    assert saved[0]["domain"] == ".google.com"
    assert os.stat(p).st_mode & 0o777 == 0o600
    # No temp file left (the lock file stays, empty).
    assert sorted(f.name for f in tmp_path.iterdir()) == [".c.json.lock", "c.json"]


async def test_from_storage_saves_rotated_cookies(tmp_path, fake, monkeypatch):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(COOKIES))
    client = GoogleChatClient.from_storage(p, http=fake_http(fake))
    fake.set_cookies = ["SIDCC=rotated; Domain=.google.com; Path=/"]
    fake.replies["paginated_world"] = gc.PaginatedWorldResponse()
    async with client:
        await client.spaces.list()
    assert json.loads(p.read_text())["SIDCC"] == "rotated"


async def test_new_thread_409_on_retry_is_already_posted(client, fake):
    fake.status_queue["create_topic"] = [(503, {}), (409, {})]
    sent = await client.messages.send("S1", "hi")
    assert (sent.id, sent.thread_id, sent.already_posted) == (None, None, True)


# ── Unusual replies ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("header, wait", [("nan", 1.0), ("inf", 1.0), ("-5", 0.0)])
async def test_odd_retry_after_values(client, fake, header, wait):
    fake.status_queue["paginated_world"] = [(429, {"Retry-After": header})]
    fake.replies["paginated_world"] = gc.PaginatedWorldResponse()
    await client.spaces.list()
    assert fake.slept == [wait]  # nan/inf fall back to the 1 s backoff; negative -> 0


def _offline(client, fake):
    def handler(request):
        if request.url.path.startswith("/api/"):
            raise httpx.ConnectError("boom")
        return fake.handler(request)
    client._transport._http._transport = httpx.MockTransport(handler)


async def test_network_error_on_a_write_says_it_may_have_applied(client, fake):
    _offline(client, fake)
    with pytest.raises(NetworkError, match="may or may not have been applied"):
        await client.messages.delete("S1", "T1", "M1", is_dm=False)
    assert fake.slept == []  # sent once, never retried


async def test_network_error_on_a_read_after_all_attempts(client, fake):
    _offline(client, fake)
    with pytest.raises(NetworkError, match="gave up after 3 attempts"):
        await client.spaces.list()
    assert fake.slept == [1.0, 2.0]


async def test_connect_offline_is_a_network_error(client):
    def handler(request):
        raise httpx.ConnectError("boom")
    client._transport._http._transport = httpx.MockTransport(handler)
    with pytest.raises(NetworkError):
        await client.connect()


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_mole_world_is_session_expired(client, status):
    client._transport._http._transport = httpx.MockTransport(lambda r: httpx.Response(status))
    with pytest.raises(SessionExpiredError):
        await client.connect()


def test_malformed_wiz_data_is_a_clean_error():
    with pytest.raises(GoogleChatError, match="WIZ_global_data"):
        parse_xsrf_token("<script>window.WIZ_global_data = {broken;</script>")


# ── Damaged cookie file ───────────────────────────────────────────────────────

@pytest.mark.parametrize("content", ["", "{not json"])
def test_load_cookies_invalid_json_is_not_signed_in(tmp_path, content):
    p = tmp_path / "c.json"
    p.write_text(content)
    with pytest.raises(NotSignedInError, match="not valid JSON"):
        load_cookies(p)


async def test_cookie_save_failure_does_not_fail_a_posted_send(tmp_path, fake):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(COOKIES))
    client = GoogleChatClient.from_storage(p, http=fake_http(fake))
    p.write_text("{damaged")  # e.g. edited by hand while the client runs
    fake.set_cookies = ["SIDCC=rotated; Domain=.google.com; Path=/"]
    resp = gc.CreateTopicResponse()
    resp.topic.id.topic_id = "T5"
    fake.replies["create_topic"] = resp
    async with client:
        sent = await client.messages.send("S1", "hi")
    assert sent.thread_id == "T5"
    assert len(fake.requests("create_topic")) == 1


async def test_cookies_of_other_google_hosts_are_not_saved(fake):
    seen = []
    fake.set_cookies = ["LSID=x; Domain=accounts.google.com; Path=/",
                        "SIDCC=new; Domain=.google.com; Path=/"]
    fake.replies["paginated_world"] = gc.PaginatedWorldResponse()
    async with GoogleChatClient(COOKIES, http=fake_http(fake), on_cookies_changed=seen.append) as c:
        await c.spaces.list()
    assert seen == [{"SIDCC": "new"}]
