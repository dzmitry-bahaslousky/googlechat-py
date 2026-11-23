from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime

from . import _convert
from ._proto import googlechat_pb2 as gc
from ._spaces import SpacesAPI
from ._transport import AlreadyPosted, Transport
from ._users import UsersAPI
from .types import Message, SentMessage, SharedItem


_NEWEST = 2**53 - 1  # list_messages "older than" bound meaning "the newest"
_THREAD_PAGE = 50
_PARALLEL_CALLS = 5
_MAX_SEARCH_PAGES = 20
_SHARED_PAGE = 40  # what the web UI asks for
_MAX_SHARED_PAGES = 25
# "Shared" panel lists, by the number the web UI sends for each (seen live 2026-11-23).
SHARED_KINDS = {"files": 6, "links": 3, "media": 5}


def _first_and_newest(msgs: list[gc.Message], topic_id: str, replies: int) -> list[gc.Message]:
    """The thread's first message plus its newest `replies` replies, oldest first."""
    unique = {m.id.message_id: m for m in msgs}
    head = unique.pop(topic_id, None)  # a thread's first message has the thread's id
    rest = sorted(unique.values(), key=lambda m: m.create_time)
    rest = rest[-replies:] if replies > 0 else []
    return ([head] if head else []) + rest


def _local_id() -> str:
    """Client-made message id. Google refuses a second message with the same
    one (409), so it must be unique per message: time plus randomness."""
    return f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"


class MessagesAPI:
    def __init__(self, transport: Transport, users: UsersAPI, spaces: SpacesAPI):
        self._t = transport
        self._users = users
        self._spaces = spaces

    async def list(self, space_id: str, *, is_dm: bool | None = None, page_size: int = 20,
                   replies: int = 10) -> list[Message]:
        """The latest `page_size` threads: each thread's first message plus its newest
        `replies` replies, oldest first within a thread."""
        is_dm = await self._spaces.resolve(space_id, is_dm)
        req = gc.ListTopicsRequest()
        req.group_id.CopyFrom(_convert.group_id(space_id, is_dm))
        req.page_size_for_topics = page_size
        req.page_size_for_replies = replies
        resp = await self._t.call("list_topics", req, gc.ListTopicsResponse)

        limit = asyncio.Semaphore(_PARALLEL_CALLS)

        async def thread(topic: gc.Topic) -> list[gc.Message]:
            msgs = list(topic.replies)
            # Which replies list_topics includes varies (live 2026-11-20): for an
            # unread thread, a run starting at the oldest unread reply; for a
            # read one, often only the newest reply. So unless the thread has no
            # replies (its newest activity is its first message) or we already
            # hold the newest `replies` replies, fetch them with list_messages.
            head = next((m for m in msgs if m.id.message_id == topic.id.topic_id), None)
            held = [m for m in msgs if m is not head]
            newest = max((m.create_time for m in msgs), default=0)
            has_replies = not head or topic.sort_time > head.create_time
            if replies > 0 and has_replies and (topic.sort_time > newest or len(held) < replies):
                async with limit:
                    page = await self._list_messages(topic.id, replies + 1)
                msgs += page.messages
            return _first_and_newest(msgs, topic.id.topic_id, replies)

        threads = await asyncio.gather(*(thread(t) for t in resp.topics))
        messages = [
            _convert.message(msg, topic.id.topic_id)
            for topic, msgs in zip(resp.topics, threads)
            for msg in msgs
        ]
        await self._users.fill_names(m.sender for m in messages)
        return messages

    async def list_thread(self, space_id: str, thread_id: str, *, is_dm: bool | None = None,
                          limit: int | None = None) -> list[Message]:
        """Every message of one thread (or its newest `limit`), oldest first."""
        is_dm = await self._spaces.resolve(space_id, is_dm)
        topic_id = _convert.topic_id(space_id, thread_id, is_dm)
        found: dict[str, gc.Message] = {}
        older_than = _NEWEST
        while limit is None or len(found) < limit:
            page = await self._list_messages(topic_id, _THREAD_PAGE, older_than)
            new = [m for m in page.messages if m.id.message_id not in found]
            # contains_first_message is not reliable (live: false after the
            # whole thread was read); a page with nothing new is the end.
            if not new:
                break
            found.update((m.id.message_id, m) for m in new)
            older_than = min(m.create_time for m in page.messages)
        ordered = sorted(found.values(), key=lambda m: m.create_time)
        if limit is not None:
            ordered = ordered[-limit:]
        messages = [_convert.message(m, thread_id) for m in ordered]
        await self._users.fill_names(m.sender for m in messages)
        return messages

    async def search(self, query: str, *, space_id: str | None = None, is_dm: bool | None = None,
                     page_size: int = 20, since: datetime | None = None,
                     until: datetime | None = None, order: str = "newest") -> list[Message]:
        """Server-side search across all your chats, or one chat with `space_id`.

        order: "newest" (newest first) or "relevance". since/until limit by date.
        """
        if order not in ("newest", "relevance"):
            raise ValueError('order must be "newest" or "relevance"')
        if space_id:
            is_dm = await self._spaces.resolve(space_id, is_dm)
        found: dict[str, gc.Message] = {}
        cursor = ""
        for _ in range(_MAX_SEARCH_PAGES):
            req = gc.SearchMessagesV2Request(size=min(max(page_size, 1), 50), query=query)
            req.filter.SetInParent()  # the web client always sends a filter
            if space_id:
                conv = req.filter.scope.conversations.add(kind=1)
                conv.group_id.CopyFrom(_convert.group_id(space_id, is_dm))
            if since:
                req.filter.dates.start_usec = _convert.to_micros(since)
            if until:
                req.filter.dates.end_usec = _convert.to_micros(until)
            # Always send a sort: without one, a scoped search is refused (400)
            # and following the cursor of a dated search fails (500).
            req.sort.operator = (gc.SearchSort.CHRONOLOGICAL if order == "newest"
                                 else gc.SearchSort.RELEVANCE)
            if cursor:
                req.cursor = cursor
            resp = await self._t.call("search_messages_v2", req, gc.SearchMessagesV2Response)
            new = [i.message for i in resp.results.items if i.message.id.message_id not in found]
            found.update((m.id.message_id, m) for m in new)  # pages can repeat hits
            if len(found) >= page_size or not new or not resp.cursor:
                break
            cursor = resp.cursor
        hits = list(found.values())
        if order == "newest":  # the server's order is only roughly chronological
            hits.sort(key=lambda m: -m.create_time)
        messages = [_convert.message(m) for m in hits[:page_size]]
        await self._users.fill_names(m.sender for m in messages)
        return messages

    async def _list_messages(self, topic_id: gc.TopicId, page_size: int,
                             older_than: int = 0) -> gc.ListMessagesResponse:
        """One page of a thread, newest first, older than `older_than` (µs)."""
        req = gc.ListMessagesRequest(page_size=page_size)
        req.parent_id.topic_id.CopyFrom(topic_id)
        req.filter.older_than = older_than or _NEWEST
        return await self._t.call("list_messages", req, gc.ListMessagesResponse)

    async def list_shared(self, space_id: str, *, is_dm: bool | None = None,
                          kind: str = "files", limit: int = 100) -> list[SharedItem]:
        """What the chat's "Shared" panel lists, newest first: kind "files" (Drive
        files and uploads), "links" or "media" (images and videos), across the
        chat's whole history, up to `limit` items (reading at most 1,000 rows)."""
        if kind not in SHARED_KINDS:
            raise ValueError(f"kind must be one of {', '.join(SHARED_KINDS)}")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        is_dm = await self._spaces.resolve(space_id, is_dm)
        group = _convert.batch_group(space_id, is_dm)
        items: list[SharedItem] = []
        drive_ids: set[str] = set()
        after = None  # the last row's message id: the next page starts after it
        for _ in range(_MAX_SHARED_PAGES):
            # Always a full page: duplicates are dropped below, so asking for only
            # the few still missing could cost one request per duplicate row.
            page, last, rows = _convert.shared_page(await self._t.batch_execute(
                "TBt4vb", [group, None, 1, _SHARED_PAGE, after, None, [SHARED_KINDS[kind]]]))
            for item in page:
                # A page lists each Drive file once, at its newest share, but a
                # later page repeats older shares of it (live 2026-11-23). Keep
                # the newest, as the web UI does.
                drive_id = getattr(item.item, "drive_id", None)
                if drive_id in drive_ids:
                    continue
                if drive_id:
                    drive_ids.add(drive_id)
                items.append(item)
            if len(items) >= limit or rows < _SHARED_PAGE or last == after:
                break
            after = last
        items = items[:limit]
        await self._users.fill_names(i.sender for i in items)
        return items

    async def send(self, space_id: str, text: str, *, is_dm: bool | None = None) -> SentMessage:
        """Post `text` as a new thread. Posts as you."""
        is_dm = await self._spaces.resolve(space_id, is_dm)
        req = gc.CreateTopicRequest()
        req.group_id.CopyFrom(_convert.group_id(space_id, is_dm))
        req.text_body = text
        req.local_id = _local_id()
        try:
            resp = await self._t.call("create_topic", req, gc.CreateTopicResponse)
        except AlreadyPosted:
            return SentMessage(id=None, thread_id=None, already_posted=True)
        topic = resp.topic
        return SentMessage(
            id=topic.replies[0].id.message_id if topic.replies else None,
            thread_id=topic.id.topic_id if resp.HasField("topic") else None,
        )

    async def reply(self, space_id: str, thread_id: str, text: str, *,
                    is_dm: bool | None = None) -> SentMessage:
        """Post `text` as a reply in a thread. Posts as you."""
        is_dm = await self._spaces.resolve(space_id, is_dm)
        req = gc.CreateMessageRequest()
        req.parent_id.topic_id.CopyFrom(_convert.topic_id(space_id, thread_id, is_dm))
        req.text_body = text
        req.local_id = _local_id()
        try:
            resp = await self._t.call("create_message", req, gc.CreateMessageResponse)
        except AlreadyPosted:
            return SentMessage(id=None, thread_id=thread_id, already_posted=True)
        return SentMessage(
            id=resp.message.id.message_id if resp.HasField("message") else None,
            thread_id=thread_id,
        )

    async def edit(self, space_id: str, thread_id: str, message_id: str, text: str, *,
                   is_dm: bool | None = None) -> SentMessage:
        """Replace the whole text of one of your own messages."""
        is_dm = await self._spaces.resolve(space_id, is_dm)
        req = gc.EditMessageRequest()
        _convert.fill_message_id(req.id, space_id, thread_id, message_id, is_dm)
        req.text_body = text
        resp = await self._t.call("edit_message", req, gc.EditMessageResponse)
        has = resp.HasField("message")
        return SentMessage(
            id=resp.message.id.message_id if has else message_id,
            thread_id=thread_id,
            last_edit_time=_convert.from_micros(resp.message.last_edit_time) if has else None,
        )

    async def delete(self, space_id: str, thread_id: str, message_id: str, *,
                     is_dm: bool | None = None) -> None:
        """Delete one of your own messages."""
        is_dm = await self._spaces.resolve(space_id, is_dm)
        req = gc.DeleteMessageRequest()
        _convert.fill_message_id(req.id, space_id, thread_id, message_id, is_dm)
        await self._t.post("delete_message", req)
