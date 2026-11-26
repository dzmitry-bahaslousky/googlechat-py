from datetime import datetime, timezone

import pytest

from googlechat import (
    Attachment, DriveFile, GoogleChatError, Link, MembersNotAddedError, SpaceNotFoundError,
    _messages,
)
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


def unread_world() -> gc.PaginatedWorldResponse:
    """Chats in each read state: S1 new message, S2 followed thread, S3 read, D1 unread
    thread, D2 never opened but empty."""
    resp = gc.PaginatedWorldResponse()
    for ts, gid, head, read, topics, thread in [
            (5, "S1", T_US + 1, T_US, 0, False), (4, "S2", T_US, T_US, 1, False),
            (3, "S3", T_US - 1, T_US, 0, False), (2, "D1", T_US, T_US, 0, True),
            (1, "D2", 0, 0, 0, False)]:
        item = resp.world_items.add(sort_timestamp=ts)
        if gid.startswith("D"):
            item.group_id.dm_id.dm_id = gid
        else:
            item.group_id.space_id.space_id = gid
        rs = item.read_state
        rs.last_head_message_create_time_usec, rs.last_read_time = head, read
        rs.unread_subscribed_topic_count, rs.has_unread_thread = topics, thread
    return resp


async def test_unread_lists_chats_with_new_messages_or_thread_replies(client, fake):
    fake.replies["paginated_world"] = unread_world()
    unread = await client.spaces.unread()
    assert [s.id for s in unread] == ["S1", "S2", "D1"]
    assert unread[0].last_read_time == T
    assert [s.unread for s in await client.spaces.list()] == [True, True, False, True, False]
    assert [s.id for s in await client.spaces.unread(page_size=2)] == ["S1", "S2"]


def sections_world(req):
    """paginated_world: sections when asked for them; else chats S1 (in Work), S2, D1 (in Hobby)."""
    resp = gc.PaginatedWorldResponse()
    if req.world_section_requests[0].fetch_roster_sections:
        sec = resp.world_section_responses.add()
        for order, ltype, key, name in [(16, gc.GroupLabelId.CUSTOM_SECTION, "k16", "Hobby"),
                                        (15, gc.GroupLabelId.CUSTOM_SECTION, "k15", "Work"),
                                        (17, gc.GroupLabelId.LABEL_TYPE_UNSPECIFIED, "", "")]:
            info = sec.roster_sections.add().info
            info.sort_order, info.name = order, name
            info.section_id.group_label_id.group_label_type = ltype
            info.section_id.group_label_id.label_secondary_key = key
        return resp
    for ts, gid, key in [(3, "S1", "k15"), (2, "S2", None), (1, "D1", "k16")]:
        item = resp.world_items.add(sort_timestamp=ts, room_name=f"Room {gid}")
        if gid.startswith("D"):
            item.group_id.dm_id.dm_id = gid
        else:
            item.group_id.space_id.space_id = gid
        item.read_state.group_label_id.add(group_label_type=gc.GroupLabelId.MUTED)
        if key:
            item.read_state.group_label_id.add(
                group_label_type=gc.GroupLabelId.CUSTOM_SECTION, label_secondary_key=key)
    return resp


async def test_sections_in_sidebar_order_with_their_chats(client, fake):
    fake.replies["paginated_world"] = sections_world

    sections = await client.spaces.sections()

    assert [(s.id, s.name, [sp.id for sp in s.spaces]) for s in sections] == [
        ("k15", "Work", ["S1"]), ("k16", "Hobby", ["D1"])]
    assert (await client.spaces.list())[1].section_id is None
    assert fake.requests("paginated_world")[0].world_section_requests[0].fetch_roster_sections


async def test_sections_by_name_or_id(client, fake):
    fake.replies["paginated_world"] = sections_world
    assert [s.name for s in await client.spaces.sections("work")] == ["Work"]
    assert [s.name for s in await client.spaces.sections("k16")] == ["Hobby"]
    fake.calls.clear()
    assert await client.spaces.sections("nope") == []
    assert len(fake.requests("paginated_world")) == 1  # no chat-list walk for no match


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


def added_reply(req: gc.CreateMembershipRequest) -> gc.CreateMembershipResponse:
    resp = gc.CreateMembershipResponse()
    for info in req.invitee_member_infos:
        membership = resp.results.add().membership
        membership.id.member_id.user_id.id = info.invitee_info.user_id.id
        membership.membership_state = gc.MEMBER_JOINED
    return resp


async def test_create_space_then_add_members(client, fake):
    created = gc.CreateGroupResponse()
    created.group.group_id.space_id.space_id = "S5"
    created.group.name = "Team"
    fake.replies["create_group"] = created
    fake.replies["create_membership"] = added_reply

    space = await client.spaces.create("Team", ["111", "222", "111"])

    assert (space.id, space.is_dm, space.name, space.member_ids) == ("S5", False, "Team",
                                                                    ["111", "222"])
    req = fake.requests("create_group")[0]
    assert req.space.name == "Team"
    assert req.space.HasField("flat_group") and req.space.HasField("space_type")
    assert req.local_id and req.should_find_existing_space is False and req.HasField(
        "should_find_existing_space")
    add = fake.requests("create_membership")[0]
    assert add.group_id.space_id.space_id == "S5"
    infos = [i.invitee_info for i in add.invitee_member_infos]
    assert [i.user_id.id for i in infos] == ["111", "222"]
    assert not any(i.HasField("invitation_mode") or i.email for i in infos)
    assert await client.spaces.resolve("S5", None) is False  # known without a listing
    assert not req.space.HasField("group_guest_access_settings")  # internal by default


async def test_create_external_space(client, fake):
    created = gc.CreateGroupResponse()
    created.group.group_id.space_id.space_id = "S5"
    fake.replies["create_group"] = created

    await client.spaces.create("Guests", external=True)

    req = fake.requests("create_group")[0]
    assert (req.space.group_guest_access_settings.guest_access_state
            == gc.GroupGuestAccessSettings.ENABLED)


async def test_create_space_refuses_emails_before_creating(client, fake):
    with pytest.raises(GoogleChatError, match="a@example.com"):
        await client.spaces.create("Team", ["111", "a@example.com"])
    assert fake.calls == []


async def test_create_space_names_the_space_when_adding_fails(client, fake):
    created = gc.CreateGroupResponse()
    created.group.group_id.space_id.space_id = "S5"
    fake.replies["create_group"] = created
    fake.status["create_membership"] = 403

    with pytest.raises(MembersNotAddedError, match="Do not create the space again") as e:
        await client.spaces.create("Team", ["111"])
    assert (e.value.space_id, e.value.missing, e.value.space_created) == ("S5", ["111"], True)


async def test_create_space_reports_people_google_skipped(client, fake):
    created = gc.CreateGroupResponse()
    created.group.group_id.space_id.space_id = "S5"
    fake.replies["create_group"] = created
    skipped = gc.CreateMembershipResponse()
    ok = skipped.results.add().membership
    ok.id.member_id.user_id.id, ok.membership_state = "111", gc.MEMBER_JOINED
    failed = skipped.results.add().membership
    failed.id.member_id.user_id.id, failed.membership_state = "222", gc.MEMBER_FAILED
    fake.replies["create_membership"] = skipped  # 333 is not in the reply at all

    with pytest.raises(MembersNotAddedError) as e:
        await client.spaces.create("Team", ["111", "222", "333"])
    assert (e.value.space_id, e.value.missing, e.value.space_created) == (
        "S5", ["222", "333"], True)


@pytest.mark.parametrize("name", ["", "   "])
async def test_create_space_refuses_a_blank_name(client, fake, name):
    with pytest.raises(GoogleChatError, match="needs a name"):
        await client.spaces.create(name)
    assert fake.calls == []


@pytest.mark.parametrize("method", ["create_group", "create_membership"])
async def test_create_space_and_add_members_are_never_retried(client, fake, method):
    created = gc.CreateGroupResponse()
    created.group.group_id.space_id.space_id = "S5"
    fake.replies["create_group"] = created
    fake.status[method] = 503  # a retry could create a second space

    with pytest.raises(GoogleChatError):
        await client.spaces.create("Team", ["111"])
    assert len(fake.requests(method)) == 1


async def test_add_members(client, fake):
    fake.replies["create_membership"] = added_reply

    assert await client.members.add("S1", ["111"]) == ["111"]
    assert await client.members.add("S1", []) == []
    with pytest.raises(GoogleChatError, match="Not numeric"):
        await client.members.add("S1", ["a@example.com"])

    [req] = fake.requests("create_membership")
    assert req.group_id.space_id.space_id == "S1"
    assert req.invitee_member_infos[0].invitee_info.user_id.id == "111"


async def test_add_members_reports_people_google_skipped(client, fake):
    fake.replies["create_membership"] = gc.CreateMembershipResponse()  # no results

    with pytest.raises(MembersNotAddedError) as e:
        await client.members.add("S1", ["111"])
    assert (e.value.space_id, e.value.missing, e.value.space_created) == ("S1", ["111"], False)


async def test_add_members_refuses_dms(client, fake):
    with pytest.raises(GoogleChatError, match="only to named spaces"):
        await client.members.add("D1", ["111"])
    assert not fake.requests("create_membership")


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


def shared_row(annotation: list, message_id: str, posted_us: int, sender: str = "u2",
               chat: list | None = None) -> list:
    """One "Shared" panel row, shaped like a live TBt4vb reply (2026-11-23)."""
    chat = chat or ["space/S1", "S1", 2]
    return [annotation, [1], [message_id, None, [f"T-{message_id}", None, chat]],
            posted_us, None, posted_us + 500, [sender, f"human/{sender}", 0]]


DOC_ROW = shared_row(
    [3, 0, 0, ["drv1", "Plan", None, "application/vnd.google-apps.document", False, True,
               None, None, True, None, None, [], "example.com"],
     None, None, None, None, None, None, None, None, None, None, None, None, None, None,
     "AATUf-x", 3], "M1", 1_700_000_000_000_000)
UPLOAD_ROW = shared_row(
    [13, 0, 0, None, None, None, None, None, "notes.md123",
     ["tok", None, "notes.md", "text/markdown", [10, 20], "notes.md123", None, 1, None, None,
      None, [3], "1700000000000001"], None, None, None, None, None, None, None, None,
     "AATUf-y", 1], "M2", 1_600_000_000_000_000, sender="u3")
LINK_ROW = shared_row(
    [1, 0, 0, None, None, None,
     [None, None, None, None, None, None, [None, None, "https://example.com/a"], "example.com",
      None, None, None, None, [None, None, "https://www.google.com/url"], None, 1, 3]],
    "M3", 1_500_000_000_000_000)
MENTION_ROW = shared_row([6, 0, 4], "M4", 1_400_000_000_000_000)  # not a file: skipped


def panel(rows: list, kind: int = 6) -> list:
    return [None, [[kind, rows]]]


def paged_panel(rows: list):
    """A TBt4vb reply function serving `rows` in pages, after the cursor message."""
    def reply(args):
        start = 0 if args[4] is None else [r[2] for r in rows].index(args[4]) + 1
        return panel(rows[start:start + args[3]])
    return reply


async def test_list_shared_reads_the_shared_panel(client, fake):
    fake.batch_replies["TBt4vb"] = panel([DOC_ROW, UPLOAD_ROW, MENTION_ROW])
    fake.replies["get_members"] = members_reply

    items = await client.messages.list_shared("S1")

    [(rpc, args)] = [c for c in fake.calls if c[0] == "TBt4vb"]
    assert args == [["space/S1", "S1", 2], None, 1, 40, None, None, [6]]
    doc, upload = items
    assert doc.item == DriveFile("doc", "Plan", "drv1", "application/vnd.google-apps.document",
                                 None, True, False)
    assert (doc.message_id, doc.thread_id, doc.sender.name) == ("M1", "T-M1", "Name u2")
    assert doc.create_time == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert upload.item == Attachment("notes.md", "text/markdown", "tok")
    assert upload.sender.id == "u3"


async def test_list_shared_links_in_a_dm(client, fake):
    fake.batch_replies["TBt4vb"] = panel([LINK_ROW], kind=3)
    [link] = await client.messages.list_shared("D1", kind="links")
    assert link.item == Link("https://example.com/a", None, None, "example.com")
    args = fake.requests("TBt4vb")[0]
    assert args[0] == ["dm/D1", "D1", 5] and args[6] == [3]


async def test_list_shared_pages_after_the_last_message(client, fake):
    rows = [shared_row(UPLOAD_ROW[0], f"M{i}", 1_600_000_000_000_000 - i) for i in range(43)]
    fake.batch_replies["TBt4vb"] = paged_panel(rows)

    items = await client.messages.list_shared("S1", limit=100)

    assert [i.message_id for i in items] == [f"M{i}" for i in range(43)]
    first, second = fake.requests("TBt4vb")
    assert (first[3], second[3], second[4]) == (40, 40, rows[39][2])



async def test_list_shared_keeps_only_the_newest_share_of_a_drive_file(client, fake, monkeypatch):
    # Live, a later page repeats older shares of a Drive file already listed.
    monkeypatch.setattr(_messages, "_SHARED_PAGE", 1)
    older_share = shared_row(DOC_ROW[0], "M0", 1_000)
    fake.batch_replies["TBt4vb"] = paged_panel([DOC_ROW, UPLOAD_ROW, older_share])

    items = await client.messages.list_shared("S1")
    assert [i.message_id for i in items] == ["M1", "M2"]


async def test_list_shared_reads_at_most_max_pages(client, fake, monkeypatch):
    # Rows we drop (here: repeats of one Drive file) don't count toward limit,
    # so the page cap is what ends a chat full of them.
    monkeypatch.setattr(_messages, "_MAX_SHARED_PAGES", 2)
    rows = [shared_row(DOC_ROW[0], f"M{i}", 1_000 - i) for i in range(200)]
    fake.batch_replies["TBt4vb"] = paged_panel(rows)
    assert len(await client.messages.list_shared("S1")) == 1
    assert len(fake.requests("TBt4vb")) == 2

async def test_list_shared_stops_at_limit(client, fake):
    fake.batch_replies["TBt4vb"] = lambda args: panel([UPLOAD_ROW] * args[3])
    items = await client.messages.list_shared("S1", limit=2)
    assert len(items) == 2
    # Always a full page, so dropped duplicates can't turn into many tiny requests.
    assert [args[3] for args in fake.requests("TBt4vb")] == [40]


async def test_list_shared_empty_chat(client, fake):
    fake.batch_replies["TBt4vb"] = [None, [[6]]]
    assert await client.messages.list_shared("S1") == []


async def test_list_shared_rejects_unknown_kind_and_bad_limit(client):
    with pytest.raises(ValueError):
        await client.messages.list_shared("S1", kind="photos")
    with pytest.raises(ValueError):
        await client.messages.list_shared("S1", limit=0)


@pytest.mark.parametrize("reply", [
    panel([[None, [1], None, 1, None, 2, None]]),  # a row with no message id
    panel([DOC_ROW[:2]]),                           # a row cut short
    [None, [None]],                                  # no list of rows
])
async def test_list_shared_unexpected_layout_is_a_library_error(client, fake, reply):
    fake.batch_replies["TBt4vb"] = reply
    with pytest.raises(GoogleChatError, match="unexpected layout"):
        await client.messages.list_shared("S1")


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
