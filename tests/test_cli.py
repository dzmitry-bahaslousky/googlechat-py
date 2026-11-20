import json
from datetime import datetime

import httpx
import pytest
from click.testing import CliRunner

from googlechat import NotSignedInError
from googlechat import cli
from googlechat import login as login_flow
from googlechat._proto import googlechat_pb2 as gc

from .conftest import FakeChat, make_client
from .test_api import (
    T_US, list_messages_reply, make_msg, members_reply, search_pages, sections_world, thread_of,
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


def test_sections_text_and_unknown_name(fake):
    fake.replies["paginated_world"] = sections_world
    r = gchat("sections")
    assert r.exit_code == 0, r.output
    assert r.output.splitlines() == ["Work  (1 chats)", "  S1  space  Room S1",
                                     "Hobby  (1 chats)", "  D1  dm     Room D1"]

    r = gchat("sections", "nope")
    assert r.exit_code != 0 and "No section named nope" in r.output


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
    assert json.loads(r.output) == {"id": "M5", "thread_id": "T5", "already_posted": False}
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


@pytest.mark.parametrize("args", [
    ("reply", "S1", "T1", "ok"),
    ("edit", "S1", "T1", "M1", "fixed"),
    ("delete", "S1", "T1", "M1"),
    ("react", "S1", "T1", "M1", "👍"),
    ("react", "S1", "T1", "M1", "👍", "--remove"),
    ("dm", "ann@example.com"),
])
def test_every_write_refuses_without_yes_when_not_interactive(fake, args):
    r = gchat(*args)
    assert r.exit_code == 1 and "--yes" in r.output
    written = {"create_message", "edit_message", "delete_message", "update_reaction",
               "create_dm_extended"}
    assert not [m for m, _ in fake.calls if m in written]


def test_dm_asks_first(fake, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    r = gchat("dm", "ann@example.com", input="n\n")
    assert "Open a DM with ann@example.com?" in r.output and r.exit_code == 1
    assert not fake.requests("create_dm_extended")


def test_edit_and_delete_prompts_show_the_message(fake, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    fake.replies["list_messages"] = list_messages_reply(thread_of(2))
    fake.replies["edit_message"] = gc.EditMessageResponse()

    r = gchat("edit", "S1", "T1", "R2", "fixed", input="y\n")
    assert r.exit_code == 0, r.output
    assert "Message R2 in space Team:\n  Ann: msg 2\nChange it to:\nfixed" in r.output

    r = gchat("delete", "S1", "T1", "R2", input="n\n")
    assert "Ann: msg 2\nDelete it?" in r.output and r.exit_code == 1
    assert not fake.requests("delete_message")


def test_control_characters_from_chat_are_shown_not_sent_to_the_terminal(fake, monkeypatch):
    msgs = thread_of(2)
    msgs[1].text_body = "hi\x1b[1A\x1b[2Kfake\x07"
    msgs[1].creator.name = "Ann\x1b]52;c;cGF3bmVk\x07"
    fake.replies["list_messages"] = list_messages_reply(msgs)

    r = gchat("thread", "S1", "T1")
    assert "\x1b" not in r.output and "\x07" not in r.output
    assert "    hi\\x1b[1A\\x1b[2Kfake\\x07" in r.output
    assert "Ann\\x1b]52;c;cGF3bmVk\\x07" in r.output

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    r = gchat("delete", "S1", "T1", "R2", input="n\n")
    assert "\x1b" not in r.output and "hi\\x1b[1A" in r.output


def test_safe_keeps_newlines_tabs_and_emoji():
    assert cli._safe("a\n\tb 👍🏽 👨‍👩‍👧 é\x00\x9b") == "a\n\tb 👍🏽 👨‍👩‍👧 é\\x00\\x9b"


def test_delete_of_a_message_not_in_the_thread_is_refused(fake, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    fake.replies["list_messages"] = list_messages_reply(thread_of(2))
    r = gchat("delete", "S1", "T1", "NOPE", input="y\n")
    assert r.exit_code == 1 and "NOPE is not in thread T1" in r.output
    assert not fake.requests("delete_message")


def test_send_already_posted_by_an_earlier_attempt(fake):
    fake.status_queue["create_topic"] = [(503, {}), (409, {})]
    r = gchat("send", "S1", "hi", "--yes")
    assert r.exit_code == 0, r.output
    assert "an earlier attempt posted it" in r.output


def test_network_error_is_a_clean_error(monkeypatch):
    def offline():
        c = make_client(FakeChat())
        c._transport._http._transport = httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom")))
        return c
    monkeypatch.setattr(cli, "make_client", offline)
    r = gchat("spaces")
    assert r.exit_code == 1 and "network error" in r.output
    assert "Traceback" not in r.output


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


# ── Logout ────────────────────────────────────────────────────────────────────

@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GCHAT_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("GCHAT_COOKIES", raising=False)
    monkeypatch.delenv("GCHAT_PROFILE", raising=False)
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "cookies.json").write_text("{}")
    return tmp_path / "cfg"


def test_logout_deletes_the_default_profile(config_dir):
    (config_dir / "browser-profile" / "Default").mkdir(parents=True)
    r = gchat("logout")
    assert r.exit_code == 0 and "Deleted saved cookies and browser profile." in r.output
    assert not (config_dir / "browser-profile").exists()
    assert not (config_dir / "cookies.json").exists()


def test_logout_deletes_a_custom_profile_that_login_created(config_dir, tmp_path, monkeypatch):
    profile = tmp_path / "custom"
    monkeypatch.setenv("GCHAT_PROFILE", str(profile))
    monkeypatch.setattr(login_flow, "_read_cookies", lambda *a, **k: [])
    login_flow.capture_cookies(config_dir / "cookies.json", profile, headless=True, timeout=1)
    assert (profile / login_flow.PROFILE_MARKER).exists()

    assert gchat("logout").exit_code == 0
    assert not profile.exists()


def test_logout_keeps_a_profile_it_did_not_create(config_dir, tmp_path, monkeypatch):
    chrome = tmp_path / "Chrome"
    (chrome / "Default").mkdir(parents=True)
    (chrome / "Default" / "Bookmarks").write_text("keep me")
    monkeypatch.setenv("GCHAT_PROFILE", str(chrome))
    monkeypatch.setattr(login_flow, "_read_cookies", lambda *a, **k: [])
    # Logging in with it must not claim it either.
    login_flow.capture_cookies(config_dir / "cookies.json", chrome, headless=True, timeout=1)

    r = gchat("logout")
    assert r.exit_code == 0 and "Left" in r.output
    assert (chrome / "Default" / "Bookmarks").read_text() == "keep me"
    assert not (config_dir / "cookies.json").exists()
