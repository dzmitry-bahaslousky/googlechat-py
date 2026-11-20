"""Typed results returned by the client.

Names follow Google's official Chat API (space, thread, message, member)
rather than the private API's group/topic terms.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class User:
    id: str | None
    name: str | None = None
    email: str | None = None
    is_bot: bool = False


@dataclass
class Space:
    """A named space, or a direct message / group chat (is_dm=True)."""
    id: str
    is_dm: bool
    name: str | None = None
    member_ids: list[str] = field(default_factory=list)
    section_id: str | None = None  # the sidebar section you filed it under


@dataclass
class Section:
    """A sidebar section you made, with its chats, most recently active first."""
    id: str
    name: str
    spaces: list[Space] = field(default_factory=list)


@dataclass
class DriveFile:
    kind: str  # file | doc | sheet | slide | form
    title: str | None
    drive_id: str | None
    mimetype: str | None
    thumbnail_url: str | None
    can_view: bool
    can_edit: bool


@dataclass
class Attachment:
    filename: str | None
    content_type: str | None
    attachment_token: str | None


@dataclass
class Link:
    url: str | None
    title: str | None
    snippet: str | None
    domain: str | None


@dataclass
class Message:
    id: str
    thread_id: str
    text: str
    sender: User
    create_time: datetime | None
    space_id: str | None = None  # the chat it was posted in
    is_dm: bool = False
    drive_files: list[DriveFile] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)


@dataclass
class SentMessage:
    """What the server returns after a send, reply or edit.

    already_posted is True when a retried send found the message already posted
    by an earlier attempt that lost its reply; id (and, for a new thread,
    thread_id) is then None because Google does not say them again.
    """
    id: str | None
    thread_id: str | None
    last_edit_time: datetime | None = None
    already_posted: bool = False


@dataclass
class UserStatus:
    user_id: str
    do_not_disturb: bool
    custom_status: str | None = None
    custom_emoji: str | None = None


@dataclass
class SharedItem:
    """A file, Drive item or link found in a message, with where it came from."""
    item: DriveFile | Attachment | Link
    sender: User
    create_time: datetime | None
    message_preview: str
