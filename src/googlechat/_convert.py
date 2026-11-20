"""Protobuf <-> library type conversion."""

from datetime import datetime, timezone

from ._proto import googlechat_pb2 as gc
from .types import Attachment, DriveFile, Link, Message, Space, User

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
        if ann.type in _DRIVE_KINDS and ann.HasField("drive_metadata"):
            dm = ann.drive_metadata
            out.drive_files.append(DriveFile(
                kind=_DRIVE_KINDS[ann.type], title=dm.title or None, drive_id=dm.id or None,
                mimetype=dm.mimetype or None, thumbnail_url=dm.thumbnail_url or None,
                can_view=dm.can_view, can_edit=dm.can_edit,
            ))
        elif ann.type in _UPLOAD_TYPES and ann.HasField("upload_metadata"):
            um = ann.upload_metadata
            out.attachments.append(Attachment(
                filename=um.content_name or None, content_type=um.content_type or None,
                attachment_token=um.attachment_token or None,
            ))
        elif ann.type == gc.URL and ann.HasField("url_metadata"):
            um = ann.url_metadata
            out.links.append(Link(
                url=um.url.url if um.HasField("url") else None, title=um.title or None,
                snippet=um.snippet or None, domain=um.domain or None,
            ))
    return out
