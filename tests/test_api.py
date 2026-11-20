from datetime import datetime, timezone

import pytest

from googlechat import Attachment, DriveFile, Link, SpaceNotFoundError
from googlechat._proto import googlechat_pb2 as gc

# 2026-01-02T03:04:05Z in microseconds
T_US = 1767323045_000000
T = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def make_msg(msg_id: str, text: str, creator_id: str, name: str = "") -> gc.Message:
    m = gc.Message(text_body=text, create_time=T_US)
    m.id.message_id = msg_id
    m.creator.user_id.id = creator_id
    if name:
        m.creator.name = name
    return m


def members_reply(req: gc.GetMembersRequest) -> gc.GetMembersResponse:
    resp = gc.GetMembersResponse()
    for mid in req.member_ids:
        u = resp.members.add().user
        u.user_id.id = mid.user_id.id
        u.name = f"Name {mid.user_id.id}"
        u.email = f"{mid.user_id.id}@example.com"
    return resp


# ── Spaces ────────────────────────────────────────────────────────────────────

async def test_spaces_list_sorts_by_recency_and_detects_dms(client, fake):
    resp = gc.PaginatedWorldResponse()
    old = resp.world_items.add(sort_timestamp=1, room_name="Old room")
    old.group_id.space_id.space_id = "S1"
    new = resp.world_items.add(sort_timestamp=2)
    new.group_id.dm_id.dm_id = "D1"
    new.dm_members.members.add().id = "u1"
    fake.replies["paginated_world"] = resp

    spaces = await client.spaces.list(page_size=5)

    assert [(s.id, s.is_dm, s.name, s.member_ids) for s in spaces] == [
        ("D1", True, None, ["u1"]),
        ("S1", False, "Old room", []),
    ]
    req = fake.requests("paginated_world")[0]
    assert req.fetch_from_user_spaces
    assert req.world_section_requests[0].page_size == 5


def world_pages(*pages):
    """paginated_world replies: one page per call, each with a next-page token but the last."""
    def reply(req):
        token = req.world_section_requests[0].pagination_token
        index = int(token) if token else 0
        resp = gc.PaginatedWorldResponse()
        for ts in pages[index]:
            resp.world_items.add(sort_timestamp=ts).group_id.space_id.space_id = f"S{ts}"
        sec = resp.world_section_responses.add()
        if index + 1 < len(pages):
            sec.has_more_items, sec.pagination_token = True, str(index + 1)
        return resp
    return reply


async def test_spaces_list_pages_until_enough(client, fake):
    fake.replies["paginated_world"] = world_pages([9, 8], [6, 7], [5])  # page 2 out of order
    spaces = await client.spaces.list(page_size=3)
    assert [s.id for s in spaces] == ["S9", "S8", "S7"]
    reqs = fake.requests("paginated_world")
    assert len(reqs) == 2 and reqs[0].world_section_requests[0].page_size == 3
    assert reqs[1].world_section_requests[0].pagination_token == "1"


async def test_spaces_list_stops_when_no_more_pages(client, fake):
    fake.replies["paginated_world"] = world_pages([2, 1])
    assert [s.id for s in await client.spaces.list(page_size=10)] == ["S2", "S1"]
    assert len(fake.requests("paginated_world")) == 1


async def test_spaces_find_walks_pages(client, fake):
    fake.replies["paginated_world"] = world_pages([9, 8], [7], [6])
    assert (await client.spaces.find("S7")).id == "S7"
    assert len(fake.requests("paginated_world")) == 2
    assert await client.spaces.find("nope") is None


async def test_create_dm_by_email_and_by_id(client, fake):
    resp = gc.CreateDmResponse()
    resp.dm.group_id.dm_id.dm_id = "D9"
    fake.replies["create_dm_extended"] = resp

    space = await client.spaces.create_dm("a@example.com")
    assert (space.id, space.is_dm) == ("D9", True)
    await client.spaces.create_dm("12345")

    by_email, by_id = fake.requests("create_dm_extended")
    assert (by_email.members[0].email, by_email.members[0].invitation_mode) == ("a@example.com", 2)
    assert (by_id.members[0].user_id.id, by_id.members[0].invitation_mode) == ("12345", 1)
    assert by_id.retention_settings.state == gc.RetentionSettings.PERMANENT
    assert (by_id.space_origin, by_id.flag_8, by_id.HasField("flag_8")) == (12, False, True)


async def test_mark_read_and_typing(client, fake):
    fake.replies["mark_group_readstate"] = gc.MarkGroupReadstateResponse()
    fake.replies["set_typing_state"] = gc.SetTypingStateResponse()

    await client.spaces.mark_read("D1", is_dm=True)
    await client.spaces.set_typing("S1", True, thread_id="T1")

    mark = fake.requests("mark_group_readstate")[0]
    assert mark.id.dm_id.dm_id == "D1" and mark.last_read_time > 0
    typing = fake.requests("set_typing_state")[0]
    assert typing.state == gc.TYPING
    assert typing.context.group_id.space_id.space_id == "S1"
    assert typing.context.topic_id.topic_id == "T1"


# ── Messages ──────────────────────────────────────────────────────────────────

def topics_reply(*_):
    resp = gc.ListTopicsResponse()
    topic = resp.topics.add()
    topic.id.topic_id = "T1"
    topic.replies.append(make_msg("M1", "Hello World", "u1", name="Ann"))
    m2 = make_msg("M2", "see doc", "u2")
    ann = m2.annotations.add(type=gc.DRIVE_DOC)
    ann.drive_metadata.id, ann.drive_metadata.title = "drv", "Plan"
    ann.drive_metadata.can_edit = True
    up = m2.annotations.add(type=gc.IMAGE)
    up.upload_metadata.content_name, up.upload_metadata.attachment_token = "a.png", "tok"
    url = m2.annotations.add(type=gc.URL)
    url.url_metadata.url.url, url.url_metadata.domain = "https://x.io", "x.io"
    topic.replies.append(m2)
    return resp


async def test_messages_list_converts_and_fills_names(client, fake):
    fake.replies["list_topics"] = topics_reply
    fake.replies["get_members"] = members_reply

    m1, m2 = await client.messages.list("S1", page_size=7)

    assert (m1.id, m1.thread_id, m1.text, m1.create_time) == ("M1", "T1", "Hello World", T)
    assert m1.sender.name == "Ann"  # already named: not looked up
    assert (m2.sender.name, m2.sender.email) == ("Name u2", "u2@example.com")
    assert m2.drive_files == [DriveFile("doc", "Plan", "drv", None, None, False, True)]
    assert m2.attachments == [Attachment("a.png", None, "tok")]
    assert m2.links == [Link("https://x.io", None, None, "x.io")]

    req = fake.requests("list_topics")[0]
    assert req.group_id.space_id.space_id == "S1"
    assert (req.page_size_for_topics, req.page_size_for_replies) == (7, 10)
    assert [m.user_id.id for m in fake.requests("get_members")[0].member_ids] == ["u2"]


async def test_user_names_are_cached(client, fake):
    fake.replies["list_topics"] = topics_reply
    fake.replies["get_members"] = members_reply
    await client.messages.list("S1")
    await client.messages.list("S1")
    assert len(fake.requests("get_members")) == 1


async def test_name_lookup_failure_does_not_fail_read(client, fake):
    fake.replies["list_topics"] = topics_reply
    fake.status["get_members"] = 500
    messages = await client.messages.list("S1")
    assert messages[1].sender.name is None


def thread_of(n: int, topic_id: str = "T1") -> list[gc.Message]:
    """A thread of n messages, oldest first: the first has the thread's id, times 1..n."""
    msgs = []
    for i in range(1, n + 1):
        m = make_msg(topic_id if i == 1 else f"R{i}", f"msg {i}", "u1", name="Ann")
        m.create_time = i
        msgs.append(m)
    return msgs


def list_messages_reply(thread: list[gc.Message]):
    """list_messages over `thread`: newest first, older than the filter bound."""
    def reply(req: gc.ListMessagesRequest) -> gc.ListMessagesResponse:
        older = [m for m in thread if m.create_time < req.filter.older_than]
        resp = gc.ListMessagesResponse()
        resp.messages.extend(sorted(older, key=lambda m: -m.create_time)[:req.page_size])
        return resp
    return reply


async def test_list_fetches_newest_replies_when_list_topics_lacks_them(client, fake):
    thread = thread_of(40)
    topics = gc.ListTopicsResponse()
    topic = topics.topics.add(sort_time=40)  # newest activity: message 40
    topic.id.topic_id = "T1"
    topic.replies.extend([thread[0], *thread[6:17]])  # what Google really sent: #1 and #7-#17
    fake.replies["list_topics"] = topics
    fake.replies["list_messages"] = list_messages_reply(thread)

    msgs = await client.messages.list("S1", replies=3)

    assert [m.text for m in msgs] == ["msg 1", "msg 38", "msg 39", "msg 40"]
    req = fake.requests("list_messages")[0]
    assert req.parent_id.topic_id.topic_id == "T1" and req.page_size == 4
    assert req.filter.older_than == 2**53 - 1


async def test_list_skips_list_messages_when_newest_is_present(client, fake):
    thread = thread_of(5)
    topics = gc.ListTopicsResponse()
    topic = topics.topics.add(sort_time=5)
    topic.id.topic_id = "T1"
    topic.replies.extend(thread)
    fake.replies["list_topics"] = topics

    msgs = await client.messages.list("S1", replies=2)

    assert [m.text for m in msgs] == ["msg 1", "msg 4", "msg 5"]
    assert not fake.requests("list_messages")


async def test_list_fetches_when_list_topics_gives_too_few_replies(client, fake):
    thread = thread_of(40)
    topics = gc.ListTopicsResponse()
    topic = topics.topics.add(sort_time=40)
    topic.id.topic_id = "T1"
    topic.replies.extend([thread[0], thread[39]])  # a read thread: first + newest only
    fake.replies["list_topics"] = topics
    fake.replies["list_messages"] = list_messages_reply(thread)

    msgs = await client.messages.list("S1", replies=3)

    assert [m.text for m in msgs] == ["msg 1", "msg 38", "msg 39", "msg 40"]


async def test_list_no_extra_call_for_threads_without_replies(client, fake):
    topics = gc.ListTopicsResponse()
    head = thread_of(1)[0]
    topic = topics.topics.add(sort_time=head.create_time)
    topic.id.topic_id = "T1"
    topic.replies.append(head)
    fake.replies["list_topics"] = topics

    assert [m.text for m in await client.messages.list("S1")] == ["msg 1"]
    assert not fake.requests("list_messages")


async def test_list_thread_pages_until_nothing_new(client, fake):
    thread = thread_of(120)
    fake.replies["list_messages"] = list_messages_reply(thread)

    msgs = await client.messages.list_thread("S1", "T1")

    assert [m.text for m in msgs] == [f"msg {i}" for i in range(1, 121)]
    assert len(fake.requests("list_messages")) == 4  # 50 + 50 + 20 + empty page
    assert fake.requests("list_messages")[1].filter.older_than == 71


async def test_list_thread_limit_keeps_newest(client, fake):
    fake.replies["list_messages"] = list_messages_reply(thread_of(120))
    msgs = await client.messages.list_thread("S1", "T1", limit=3)
    assert [m.text for m in msgs] == ["msg 118", "msg 119", "msg 120"]
    assert len(fake.requests("list_messages")) == 1


def search_pages(*pages):
    """search_messages_v2 replies: page i holds the given (message id, time) pairs."""
    def reply(req):
        index = int(req.cursor) if req.cursor else 0
        resp = gc.SearchMessagesV2Response()
        for mid, t in pages[index]:
            m = make_msg(mid, f"text {mid}", "u2")
            m.create_time = t
            m.id.parent_id.topic_id.topic_id = "T" + mid
            m.id.parent_id.topic_id.group_id.dm_id.dm_id = "D1"
            resp.results.items.add().message.CopyFrom(m)
        if index + 1 < len(pages):
            resp.cursor = str(index + 1)
        return resp
    return reply


async def test_search_pages_dedupes_sorts_and_names(client, fake):
    fake.replies["search_messages_v2"] = search_pages([("a", 3), ("b", 5)], [("b", 5), ("c", 9)], [("d", 1)])
    fake.replies["get_members"] = members_reply

    msgs = await client.messages.search("release", page_size=3)

    assert [m.id for m in msgs] == ["c", "b", "a"]  # newest first, "b" only once
    assert (msgs[0].space_id, msgs[0].is_dm, msgs[0].thread_id) == ("D1", True, "Tc")
    assert msgs[0].sender.name == "Name u2"
    first, second = fake.requests("search_messages_v2")
    assert first.query == "release" and first.HasField("filter")
    assert first.sort.operator == gc.SearchSort.CHRONOLOGICAL  # always sent
    assert second.cursor == "1"


async def test_search_stops_without_cursor(client, fake):
    fake.replies["search_messages_v2"] = search_pages([("a", 3)])
    fake.replies["get_members"] = members_reply
    assert [m.id for m in await client.messages.search("x", page_size=10)] == ["a"]
    assert len(fake.requests("search_messages_v2")) == 1


async def test_search_scope_dates_and_relevance(client, fake):
    fake.replies["search_messages_v2"] = search_pages([])
    since = datetime(2026, 11, 1, tzinfo=timezone.utc)
    until = datetime(2026, 11, 8, tzinfo=timezone.utc)

    await client.messages.search("x", space_id="S1", since=since, until=until, order="relevance")

    req = fake.requests("search_messages_v2")[0]
    conv = req.filter.scope.conversations[0]
    assert (conv.group_id.space_id.space_id, conv.kind) == ("S1", 1)
    assert req.filter.dates.start_usec == int(since.timestamp() * 1e6)
    assert req.filter.dates.end_usec == int(until.timestamp() * 1e6)
    assert req.sort.operator == gc.SearchSort.RELEVANCE


async def test_list_shared(client, fake):
    fake.replies["list_topics"] = topics_reply
    fake.replies["get_members"] = members_reply
    items = await client.messages.list_shared("S1")
    assert [type(i.item) for i in items] == [DriveFile, Attachment, Link]
    assert items[0].sender.name == "Name u2" and items[0].message_preview == "see doc"


async def test_send_creates_topic(client, fake):
    resp = gc.CreateTopicResponse()
    resp.topic.id.topic_id = "T5"
    resp.topic.replies.add().id.message_id = "M5"
    fake.replies["create_topic"] = resp

    sent = await client.messages.send("D1", "hi", is_dm=True)

    assert (sent.id, sent.thread_id) == ("M5", "T5")
    req = fake.requests("create_topic")[0]
    assert req.group_id.dm_id.dm_id == "D1" and req.text_body == "hi" and req.local_id


async def test_reply_creates_message_in_thread(client, fake):
    resp = gc.CreateMessageResponse()
    resp.message.id.message_id = "M6"
    fake.replies["create_message"] = resp

    sent = await client.messages.reply("S1", "T1", "yo")

    assert (sent.id, sent.thread_id) == ("M6", "T1")
    req = fake.requests("create_message")[0]
    assert req.parent_id.topic_id.topic_id == "T1"
    assert req.parent_id.topic_id.group_id.space_id.space_id == "S1"


async def test_edit_and_delete(client, fake):
    resp = gc.EditMessageResponse()
    resp.message.id.message_id = "M1"
    resp.message.last_edit_time = T_US
    fake.replies["edit_message"] = resp

    edited = await client.messages.edit("S1", "T1", "M1", "new text")
    await client.messages.delete("S1", "T1", "M1")

    assert edited.last_edit_time == T
    edit = fake.requests("edit_message")[0]
    assert (edit.id.message_id, edit.id.parent_id.topic_id.topic_id, edit.text_body) == (
        "M1", "T1", "new text")
    delete = fake.requests("delete_message")[0]
    assert delete.id.parent_id.topic_id.group_id.space_id.space_id == "S1"


# ── Reactions, members, users ─────────────────────────────────────────────────

async def test_reactions(client, fake):
    fake.replies["update_reaction"] = gc.UpdateReactionResponse()
    await client.reactions.add("S1", "T1", "M1", "👍")
    await client.reactions.remove("S1", "T1", "M1", "👍")
    add, remove = fake.requests("update_reaction")
    assert (add.option, remove.option) == (gc.ADD, gc.REMOVE)
    assert add.emoji.unicode == "👍" and add.message_id.message_id == "M1"


async def test_members_list_resolves_bare_memberships(client, fake):
    resp = gc.ListMembersResponse()
    u = resp.members.add().user
    u.user_id.id, u.name = "u1", "Ann"
    resp.memberships.add().id.member_id.user_id.id = "u1"  # already listed
    resp.memberships.add().id.member_id.user_id.id = "u2"
    fake.replies["list_members"] = resp
    fake.replies["get_members"] = members_reply

    users = await client.members.list("S1")

    assert [(x.id, x.name) for x in users] == [("u1", "Ann"), ("u2", "Name u2")]
    assert fake.requests("list_members")[0].group_id.space_id.space_id == "S1"


async def test_get_status(client, fake):
    resp = gc.GetUserStatusResponse()
    s = resp.user_statuses.add()
    s.user_id.id = "u1"
    s.dnd_settings.dnd_state = gc.DndSettings.DND
    s.custom_status.status_text = "Lunch"
    resp.user_statuses.add().user_id.id = "u2"
    fake.replies["get_user_status"] = resp

    statuses = await client.users.get_status(["u1", "u2", "u3"])

    assert list(statuses) == ["u1", "u2", "u3"]  # every requested id, in order
    assert (statuses["u1"].do_not_disturb, statuses["u1"].custom_status) == (True, "Lunch")
    assert (statuses["u2"].do_not_disturb, statuses["u2"].custom_status) == (False, None)
    assert statuses["u3"] is None  # Google returned nothing for u3


# ── DM or space ───────────────────────────────────────────────────────────────

async def test_is_dm_is_looked_up_once_when_left_out(client, fake):
    await client.spaces.mark_read("D1")
    await client.spaces.mark_read("S1")
    reqs = fake.requests("mark_group_readstate")
    assert reqs[0].id.dm_id.dm_id == "D1" and reqs[1].id.space_id.space_id == "S1"
    assert len(fake.requests("paginated_world")) == 1  # one chat-list walk, then cached


async def test_explicit_is_dm_needs_no_lookup(client, fake):
    await client.spaces.mark_read("X1", is_dm=True)
    assert not fake.requests("paginated_world")


async def test_unknown_space_without_is_dm(client, fake):
    with pytest.raises(SpaceNotFoundError):
        await client.spaces.mark_read("X1")
    assert not fake.requests("mark_group_readstate")
