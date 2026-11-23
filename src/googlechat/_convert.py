"""Protobuf <-> library type conversion."""

from datetime import datetime, timezone
from typing import Any

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message as ProtoMessage

from ._proto import googlechat_pb2 as gc
from .exceptions import GoogleChatError
from .types import Attachment, DriveFile, Link, Message, SharedItem, Space, User

_DRIVE_KINDS = {
    gc.DRIVE_FILE: "file", gc.DRIVE_DOC: "doc", gc.DRIVE_SHEET: "sheet",
    gc.DRIVE_SLIDE: "slide", gc.DRIVE_FORM: "form",
}
_UPLOAD_TYPES = (gc.UPLOAD_METADATA, gc.IMAGE, gc.PDF)


def group_id(space_id: str, is_dm: bool) -> gc.GroupId:
    g = gc.GroupId()
    if is_dm:
        g.dm_id.dm_id = space_id
    else:
        g.space_id.space_id = space_id
    return g


def batch_group(space_id: str, is_dm: bool) -> list:
    """A chat as batchexecute calls name it (seen live 2026-11-23)."""
    return [f"dm/{space_id}", space_id, 5] if is_dm else [f"space/{space_id}", space_id, 2]


def from_group_id(g: gc.GroupId) -> tuple[str, bool]:
    """(space_id, is_dm) from a GroupId; the reverse of group_id()."""
    if g.HasField("dm_id"):
        return g.dm_id.dm_id, True
    return g.space_id.space_id, False


def topic_id(space_id: str, thread_id: str, is_dm: bool) -> gc.TopicId:
    t = gc.TopicId(topic_id=thread_id)
    t.group_id.CopyFrom(group_id(space_id, is_dm))
    return t


def fill_message_id(target: gc.MessageId, space_id: str, thread_id: str,
                    message_id: str, is_dm: bool) -> None:
    target.message_id = message_id
    target.parent_id.topic_id.CopyFrom(topic_id(space_id, thread_id, is_dm))


def to_micros(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000)


def from_micros(us: int) -> datetime | None:
    """Chat timestamps are microseconds since the Unix epoch; 0 means unset."""
    return datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc) if us else None


def user(u: gc.User) -> User:
    """A member profile (get_members / list_members) as a User."""
    return User(id=u.user_id.id or None, name=u.name or None, email=u.email or None,
                is_bot=u.HasField("bot_info"))


def space(item: gc.WorldItemLite) -> Space:
    space_id, is_dm = from_group_id(item.group_id)
    rs = item.read_state
    # Google leaves unread_message_count at 0 for the web client, so compare the
    # newest message's time with your read marker, and check followed threads.
    unread = (rs.last_head_message_create_time_usec > rs.last_read_time
              or rs.unread_subscribed_topic_count > 0 or rs.has_unread_thread)
    return Space(
        id=space_id,
        is_dm=is_dm,
        name=item.room_name or item.name_users.group_name or None,
        member_ids=[u.id for u in item.dm_members.members],
        section_id=next((label.label_secondary_key for label in item.read_state.group_label_id
                         if label.group_label_type == gc.GroupLabelId.CUSTOM_SECTION), None),
        unread=unread,
        last_read_time=from_micros(rs.last_read_time),
    )


def message(msg: gc.Message, thread_id: str = "") -> Message:
    out = Message(
        id=msg.id.message_id,
        thread_id=thread_id or msg.id.parent_id.topic_id.topic_id,
        text=msg.text_body,
        sender=User(
            id=msg.creator.user_id.id or None,
            name=msg.creator.name or None,
            email=msg.creator.email or None,
        ),
        create_time=from_micros(msg.create_time),
    )
    group = msg.id.parent_id.topic_id.group_id
    if group.HasField("dm_id") or group.HasField("space_id"):
        out.space_id, out.is_dm = from_group_id(group)
    for ann in msg.annotations:
        item = annotation(ann)
        if isinstance(item, DriveFile):
            out.drive_files.append(item)
        elif isinstance(item, Attachment):
            out.attachments.append(item)
        elif isinstance(item, Link):
            out.links.append(item)
    return out


def annotation(ann: gc.Annotation) -> DriveFile | Attachment | Link | None:
    """The Drive file, upload or link an annotation stands for; None for others
    (mentions, formatting, ...)."""
    if ann.type in _DRIVE_KINDS and ann.HasField("drive_metadata"):
        dm = ann.drive_metadata
        return DriveFile(
            kind=_DRIVE_KINDS[ann.type], title=dm.title or None, drive_id=dm.id or None,
            mimetype=dm.mimetype or None, thumbnail_url=dm.thumbnail_url or None,
            can_view=dm.can_view, can_edit=dm.can_edit,
        )
    if ann.type in _UPLOAD_TYPES and ann.HasField("upload_metadata"):
        um = ann.upload_metadata
        return Attachment(
            filename=um.content_name or None, content_type=um.content_type or None,
            attachment_token=um.attachment_token or None,
        )
    if ann.type == gc.URL and ann.HasField("url_metadata"):
        um = ann.url_metadata
        return Link(
            url=um.url.url if um.HasField("url") else None, title=um.title or None,
            snippet=um.snippet or None, domain=um.domain or None,
        )
    return None


def from_jspb(data: list, msg: ProtoMessage) -> ProtoMessage:
    """Fill `msg` from JSPB, the JSON-array form of protobuf the web UI's
    batchexecute calls use: item i is field number i + 1, and a trailing
    {"<number>": value} object holds high field numbers.

    Values that don't fit our reverse-engineered schema (unknown field numbers,
    wrong types) are skipped, so a schema gap loses a field, not the whole item.
    """
    fields = dict(enumerate(data, start=1))
    if data and isinstance(data[-1], dict):
        del fields[len(data)]
        fields.update((int(k), v) for k, v in data[-1].items() if k.isdigit())
    for number, value in fields.items():
        field = msg.DESCRIPTOR.fields_by_number.get(number)
        if field is None or value is None:
            continue
        for v in (value if isinstance(value, list) else []) if field.is_repeated else [value]:
            _jspb_put(msg, field, v)
    return msg


def _jspb_put(msg: ProtoMessage, field: FieldDescriptor, value: Any) -> None:
    """Set one JSPB value (append it, for a repeated field); skip it if it doesn't fit."""
    target = getattr(msg, field.name)
    if field.type == FieldDescriptor.TYPE_MESSAGE:
        if isinstance(value, list):
            from_jspb(value, target.add() if field.is_repeated else target)
        return
    try:
        value = _jspb_scalar(field, value)
        if field.is_repeated:
            target.append(value)
        else:
            setattr(msg, field.name, value)
    except (TypeError, ValueError):
        pass


def _jspb_scalar(field: FieldDescriptor, value: Any) -> Any:
    # JSPB writes 64-bit integers as strings, and bools sometimes as 0/1.
    if field.type == FieldDescriptor.TYPE_BOOL and isinstance(value, int):
        return bool(value)
    if field.cpp_type in (FieldDescriptor.CPPTYPE_INT64, FieldDescriptor.CPPTYPE_UINT64) \
            and isinstance(value, str):
        return int(value)
    return value


def shared_page(reply: Any) -> tuple[list[SharedItem], Any, int]:
    """One "Shared" panel reply (batchexecute TBt4vb) as (the items we convert,
    the last row's message id, which is the next page's cursor, the row count).

    Reply layout (seen live 2026-11-23): [?, [[kind, [row, ...]]]]; an empty
    list has no rows entry.
    """
    try:
        rows = reply[1][0][1] if reply and reply[1] and len(reply[1][0]) > 1 else []
        items = [item for item in map(_shared_item, rows) if item]
        return items, (rows[-1][2] if rows else None), len(rows)
    except (TypeError, IndexError, KeyError) as e:
        raise GoogleChatError(
            f"Shared panel reply has an unexpected layout; Google changed it? ({e!r})"
        ) from None


def _shared_item(row: list) -> SharedItem | None:
    """One row of the "Shared" panel (batchexecute TBt4vb), or None if its kind
    is one we don't convert.

    Row layout (seen live 2026-11-23): [annotation, ?, message id, posted time
    (µs), ?, updated time (µs), sender]. The message id is
    [message_id, ?, [thread_id, ?, ["space/<id>" or "dm/<id>", id, 2 or 5]]];
    the sender is [user_id, "human/<user_id>", ?].
    """
    item = annotation(from_jspb(row[0], gc.Annotation()))
    if item is None:
        return None
    msg_id = row[2]
    thread_id = msg_id[2][0] if len(msg_id) > 2 and msg_id[2] else None
    sender = row[6][0] if len(row) > 6 and row[6] else None
    return SharedItem(item=item, sender=User(id=sender),
                      create_time=from_micros(int(row[3] or 0)),
                      message_id=msg_id[0], thread_id=thread_id)
