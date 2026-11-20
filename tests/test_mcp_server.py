import json
from datetime import datetime, timezone

import pytest

from googlechat import APIError, GoogleChatClient, Message, SessionExpiredError, User
from googlechat import mcp_server
from googlechat.serialize import to_jsonable
from googlechat._proto import googlechat_pb2 as gc

EXPECTED_TOOLS = {
    "list_spaces", "read_messages", "search_messages", "list_space_files", "list_members",
    "get_user_status", "read_thread", "send_message", "reply_message", "edit_message", "delete_message",
    "create_dm", "add_reaction", "remove_reaction", "set_typing", "mark_as_read", "sign_in",
}


@pytest.fixture
def use_client(client, monkeypatch):
    monkeypatch.setattr(mcp_server, "_client", client)
    return client


def tool_json(result) -> object:
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


async def test_all_tools_registered():
    assert {t.name for t in await mcp_server.mcp.list_tools()} == EXPECTED_TOOLS


async def test_list_spaces_tool_returns_compact_json(use_client, fake):
    resp = gc.PaginatedWorldResponse()
    item = resp.world_items.add(sort_timestamp=1, room_name="Team")
    item.group_id.space_id.space_id = "S1"
    fake.replies["paginated_world"] = resp

    result = await mcp_server.mcp.call_tool("list_spaces", {})

    assert tool_json(result) == [{"id": "S1", "is_dm": False, "name": "Team"}]


async def test_empty_list_still_returns_json(use_client, fake):
    fake.replies["paginated_world"] = gc.PaginatedWorldResponse()
    assert tool_json(await mcp_server.mcp.call_tool("list_spaces", {})) == []


async def test_reply_tool_maps_arguments(use_client, fake):
    fake.replies["create_message"] = gc.CreateMessageResponse()
    await mcp_server.mcp.call_tool(
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
    await mcp_server.mcp.call_tool(
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
