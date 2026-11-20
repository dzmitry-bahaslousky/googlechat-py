import json
from datetime import datetime

import pytest
from click.testing import CliRunner

from googlechat import NotSignedInError
from googlechat import cli
from googlechat._proto import googlechat_pb2 as gc

from .conftest import FakeChat, make_client
from .test_api import (
    T_US, list_messages_reply, make_msg, members_reply, search_pages, thread_of,
)


@pytest.fixture
def fake(monkeypatch) -> FakeChat:
    fake = FakeChat()
    monkeypatch.setattr(cli, "make_client", lambda: make_client(fake))
    world = gc.PaginatedWorldResponse()
    room = world.world_items.add(sort_timestamp=2, room_name="Team")
    room.group_id.space_id.space_id = "S1"
    world.world_items.add(sort_timestamp=1).group_id.dm_id.dm_id = "D1"
    fake.replies["paginated_world"] = world
    return fake


def gchat(*args, input=None):
    return CliRunner().invoke(cli.main, list(args), input=input)


# ── Reads ─────────────────────────────────────────────────────────────────────

def test_spaces_text_and_json(fake):
    r = gchat("spaces", "-n", "5")
    assert r.exit_code == 0, r.output
    assert r.output.splitlines() == ["S1  space  Team", "D1  dm     (unnamed)"]

    r = gchat("--json", "spaces")
    assert json.loads(r.output) == [{"id": "S1", "is_dm": False, "name": "Team"},
                                    {"id": "D1", "is_dm": True}]


def test_read_looks_up_space_kind_and_prints_threads(fake):
    topics = gc.ListTopicsResponse()
    topic = topics.topics.add()
    topic.id.topic_id = "T1"
    m = make_msg("M1", "line one\nline two", "u1", name="Ann")
    ann = m.annotations.add(type=gc.URL)
    ann.url_metadata.url.url = "https://x.io"
    topic.replies.append(m)
    fake.replies["list_topics"] = topics

    r = gchat("read", "D1", "-n", "3")

    assert r.exit_code == 0, r.output
    when = datetime.fromtimestamp(T_US / 1e6).strftime("%Y-%m-%d %H:%M")  # local time
    assert r.output.splitlines() == [
        "", "thread T1", f"  {when}  Ann  [M1]",
        "    line one", "    line two", "    link: https://x.io",
    ]
    req = fake.requests("list_topics")[0]
    assert req.group_id.dm_id.dm_id == "D1" and req.page_size_for_topics == 3


def test_explicit_kind_skips_lookup(fake):
    fake.replies["list_topics"] = gc.ListTopicsResponse()
    assert gchat("read", "X9", "--space").exit_code == 0
    assert not fake.requests("paginated_world")
    assert fake.requests("list_topics")[0].group_id.space_id.space_id == "X9"


def test_unknown_space_is_an_error(fake):
    r = gchat("read", "NOPE")
    assert r.exit_code == 1 and "not in your chat list" in r.output


def test_status_shows_users_without_status(fake):
    resp = gc.GetUserStatusResponse()
    s = resp.user_statuses.add()
    s.user_id.id = "u1"
    s.dnd_settings.dnd_state = gc.DndSettings.DND
    s.custom_status.status_text = "Lunch"
    fake.replies["get_user_status"] = resp

    r = gchat("status", "u1", "u2")
    assert r.output.splitlines() == ["u1  do not disturb  Lunch", "u2  (no status)"]

    r = gchat("--json", "status", "u1", "u2")
    assert json.loads(r.output)["no_status"] == ["u2"]


def test_not_signed_in_is_a_clean_error(monkeypatch):
    def no_cookies():
        raise NotSignedInError("Not signed in: no cookies")
    monkeypatch.setattr(cli, "make_client", no_cookies)
    r = gchat("spaces")
    assert r.exit_code == 1 and "Error: Not signed in" in r.output


# ── Writes ────────────────────────────────────────────────────────────────────

def sent_reply() -> gc.CreateTopicResponse:
    resp = gc.CreateTopicResponse()
    resp.topic.id.topic_id = "T5"
    resp.topic.replies.add().id.message_id = "M5"
    return resp


def test_send_refuses_without_yes_when_not_interactive(fake):
    r = gchat("send", "S1", "hi")
    assert r.exit_code == 1 and "--yes" in r.output
    assert not fake.requests("create_topic")


def test_send_asks_and_aborts_on_no(fake, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    r = gchat("send", "S1", "hi", input="n\n")
    assert "Send to space Team:" in r.output and r.exit_code == 1
    assert not fake.requests("create_topic")


def test_send_asks_and_sends_on_yes(fake, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    fake.replies["create_topic"] = sent_reply()
    r = gchat("send", "S1", "hi", input="y\n")
    assert r.exit_code == 0, r.output
    assert "Sent: thread T5 message M5" in r.output
    assert fake.requests("create_topic")[0].text_body == "hi"


def test_send_text_from_stdin_with_yes(fake):
    fake.replies["create_topic"] = sent_reply()
    r = gchat("--json", "send", "D1", "-", "--yes", input="multi\nline\n")
    assert json.loads(r.output) == {"id": "M5", "thread_id": "T5"}
    req = fake.requests("create_topic")[0]
    assert req.text_body == "multi\nline" and req.group_id.dm_id.dm_id == "D1"


def test_reply_edit_delete_react(fake):
    fake.replies["create_message"] = gc.CreateMessageResponse()
    fake.replies["edit_message"] = gc.EditMessageResponse()
    fake.replies["update_reaction"] = gc.UpdateReactionResponse()

    assert gchat("reply", "S1", "T1", "ok", "-y").exit_code == 0
    assert gchat("edit", "S1", "T1", "M1", "fixed", "-y").exit_code == 0
    assert gchat("delete", "S1", "T1", "M1", "-y").exit_code == 0
    assert gchat("react", "S1", "T1", "M1", "👍", "-y").exit_code == 0
    assert gchat("react", "S1", "T1", "M1", "👍", "--remove", "-y").exit_code == 0

    assert fake.requests("create_message")[0].parent_id.topic_id.topic_id == "T1"
    assert fake.requests("edit_message")[0].text_body == "fixed"
    assert fake.requests("delete_message")[0].id.message_id == "M1"
    assert [r.option for r in fake.requests("update_reaction")] == [gc.ADD, gc.REMOVE]


# ── Search and threads ────────────────────────────────────────────────────────

def test_search_all_chats_prints_where_each_hit_is(fake):
    fake.replies["search_messages_v2"] = search_pages([("a", T_US)])
    fake.replies["get_members"] = members_reply

    r = gchat("search", "release", "--since", "2026-11-01")

    assert r.exit_code == 0, r.output
    assert "dm D1  thread Ta" in r.output and "Name u2  [a]" in r.output
    req = fake.requests("search_messages_v2")[0]
    assert req.query == "release" and not req.filter.HasField("scope")
    assert req.filter.dates.start_usec == int(datetime(2026, 11, 1).astimezone().timestamp() * 1e6)
    assert not fake.requests("paginated_world")  # no space lookup without --in


def test_search_in_one_space(fake):
    fake.replies["search_messages_v2"] = search_pages([])
    r = gchat("search", "x", "--in", "S1", "--relevance")
    assert r.exit_code == 0, r.output
    req = fake.requests("search_messages_v2")[0]
    assert req.filter.scope.conversations[0].group_id.space_id.space_id == "S1"
    assert req.sort.operator == gc.SearchSort.RELEVANCE


def test_thread_command(fake):
    fake.replies["list_messages"] = list_messages_reply(thread_of(3))
    r = gchat("--json", "thread", "S1", "T1", "-n", "2")
    assert r.exit_code == 0, r.output
    assert [m["text"] for m in json.loads(r.output)] == ["msg 2", "msg 3"]
