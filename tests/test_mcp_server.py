import asyncio
import json
from datetime import datetime, timezone

import pytest

from googlechat import APIError, GoogleChatClient, Message, SessionExpiredError, User
from googlechat import mcp_server
from googlechat.serialize import to_jsonable
from googlechat._proto import googlechat_pb2 as gc

from .test_api import DOC_ROW, panel, sections_world, unread_world

READ_TOOLS = {
    "list_spaces", "list_unread", "list_sections", "read_messages", "search_messages", "list_space_files", "list_members",
    "get_user_status", "read_thread", "sign_in",
}
WRITE_TOOLS = {
    "send_message", "reply_message", "edit_message", "delete_message", "create_dm",
    "create_space", "add_members",
    "add_reaction", "remove_reaction", "set_typing", "mark_as_read",
}
writable = mcp_server.build_server(allow_writes=True)


@pytest.fixture
def use_client(client, monkeypatch):
    monkeypatch.setattr(mcp_server, "_client", client)
    return client


def tool_json(result) -> object:
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


async def test_write_tools_are_off_by_default():
    assert {t.name for t in await mcp_server.mcp.list_tools()} == READ_TOOLS


async def test_write_tools_on_when_allowed():
    assert {t.name for t in await writable.list_tools()} == READ_TOOLS | WRITE_TOOLS


async def test_list_spaces_tool_returns_compact_json(use_client, fake):
    resp = gc.PaginatedWorldResponse()
    item = resp.world_items.add(sort_timestamp=1, room_name="Team")
    item.group_id.space_id.space_id = "S1"
    fake.replies["paginated_world"] = resp

    result = await mcp_server.mcp.call_tool("list_spaces", {})

    assert tool_json(result) == [{"id": "S1", "is_dm": False, "name": "Team", "unread": False}]


async def test_list_sections_tool(use_client, fake):
    fake.replies["paginated_world"] = sections_world
    result = await mcp_server.mcp.call_tool("list_sections", {"name": "Work"})
    assert tool_json(result) == [{"id": "k15", "name": "Work", "spaces": [
        {"id": "S1", "is_dm": False, "name": "Room S1", "section_id": "k15",
         "unread": False}]}]


async def test_list_unread_tool(use_client, fake):
    fake.replies["paginated_world"] = unread_world()
    result = await mcp_server.mcp.call_tool("list_unread", {})
    assert [s["id"] for s in tool_json(result)] == ["S1", "S2", "D1"]
    assert tool_json(result)[0]["last_read_time"] == "2026-01-02T03:04:05+00:00"


async def test_empty_list_still_returns_json(use_client, fake):
    fake.replies["paginated_world"] = gc.PaginatedWorldResponse()
    assert tool_json(await mcp_server.mcp.call_tool("list_spaces", {})) == []


async def test_reply_tool_maps_arguments(use_client, fake):
    fake.replies["create_message"] = gc.CreateMessageResponse()
    await writable.call_tool(
        "reply_message", {"space_id": "D1", "thread_id": "T1", "text": "ok", "is_dm": True})
    req = fake.requests("create_message")[0]
    assert req.parent_id.topic_id.group_id.dm_id.dm_id == "D1"


async def test_auth_failure_drops_client(use_client, fake):
    fake.status["paginated_world"] = 401
    with pytest.raises(SessionExpiredError):
        await mcp_server._run(lambda c: c.spaces.list())
    assert mcp_server._client is None


async def test_bad_request_keeps_client(use_client, fake):
    fake.status["paginated_world"] = 400
    with pytest.raises(APIError):
        await mcp_server._run(lambda c: c.spaces.list())
    assert mcp_server._client is use_client


async def test_tools_look_up_is_dm_when_left_out(use_client, fake):
    fake.replies["create_message"] = gc.CreateMessageResponse()
    await writable.call_tool(
        "reply_message", {"space_id": "D1", "thread_id": "T1", "text": "ok"})
    req = fake.requests("create_message")[0]
    assert req.parent_id.topic_id.group_id.dm_id.dm_id == "D1"


def test_dump_drops_empty_fields_and_formats_dates():
    m = Message(id="M1", thread_id="T1", text="", sender=User(id="u1"),
                create_time=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert to_jsonable(m) == {
        "id": "M1", "thread_id": "T1", "sender": {"id": "u1", "is_bot": False}, "is_dm": False,
        "create_time": "2026-01-01T00:00:00+00:00",
    }


def test_client_type_is_exported():
    assert mcp_server.GoogleChatClient is GoogleChatClient


async def test_get_user_status_tool_lists_users_without_status(use_client, fake):
    resp = gc.GetUserStatusResponse()
    resp.user_statuses.add().user_id.id = "u1"
    fake.replies["get_user_status"] = resp
    result = tool_json(await mcp_server.mcp.call_tool("get_user_status", {"user_ids": ["u1", "u2"]}))
    assert result == {"statuses": [{"user_id": "u1", "do_not_disturb": False}],
                      "no_status": ["u2"]}


async def test_drop_lets_calls_in_flight_finish(use_client, fake):
    fake.replies["paginated_world"] = gc.PaginatedWorldResponse()
    release = asyncio.Event()

    async def slow(c):
        await release.wait()
        return await c.spaces.list()

    async def expired(c):
        raise SessionExpiredError("expired")

    running = asyncio.create_task(mcp_server._run(slow))
    await asyncio.sleep(0)
    with pytest.raises(SessionExpiredError):
        await mcp_server._run(expired)
    assert mcp_server._client is None
    assert not use_client._transport._http.is_closed  # still in use by `slow`
    assert use_client._transport._on_cookies_changed is None  # its cookies are stale

    release.set()
    assert json.loads(await running) == []
    assert use_client._transport._http.is_closed  # closed after its last call


async def test_read_only_server_tells_the_agent_how_to_enable_writes():
    assert "GCHAT_MCP_WRITE=1" in mcp_server.mcp.instructions


async def test_list_space_files_tool(use_client, fake):
    fake.batch_replies["TBt4vb"] = panel([DOC_ROW])
    result = await mcp_server.mcp.call_tool("list_space_files", {"space_id": "S1", "limit": 3})
    [item] = tool_json(result)
    assert item["type"] == "drive_file" and item["title"] == "Plan"
    assert item["sender"] == {"id": "u2", "is_bot": False} and item["message_id"] == "M1"
    assert fake.requests("TBt4vb")[0][3] == 40
