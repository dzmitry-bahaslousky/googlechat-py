"""Operations recorded from a live account, and scrub-proof summaries of their results.

The recorder runs each op live and saves its summary next to the scrubbed
responses; the replay test runs the same op against those responses and expects
the same summary. Summaries use only counts and years, which scrubbing keeps.
"""

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "live"


def _messages(msgs) -> dict:
    return {
        "messages": len(msgs),
        "threads": len({m.thread_id for m in msgs}),
        "named_senders": sum(bool(m.sender.name) for m in msgs),
        "sender_emails": sum(bool(m.sender.email) for m in msgs),
        "drive_files": sum(len(m.drive_files) for m in msgs),
        "attachments": sum(len(m.attachments) for m in msgs),
        "links": sum(len(m.links) for m in msgs),
        "years": sorted({m.create_time.year for m in msgs if m.create_time}),
    }


def _search(msgs) -> dict:
    return {
        **_messages(msgs),
        "chats": len({m.space_id for m in msgs}),
        "dm_hits": sum(m.is_dm for m in msgs),
        "newest_first": [m.create_time for m in msgs] == sorted(
            (m.create_time for m in msgs), reverse=True),
    }


def _spaces(spaces) -> dict:
    return {
        "spaces": len(spaces),
        "dms": sum(s.is_dm for s in spaces),
        "named": sum(bool(s.name) for s in spaces),
        "with_member_ids": sum(bool(s.member_ids) for s in spaces),
    }


def _users(users) -> dict:
    return {
        "users": len(users),
        "named": sum(bool(u.name) for u in users),
        "emails": sum(bool(u.email) for u in users),
        "bots": sum(u.is_bot for u in users),
    }


def _statuses(statuses: dict) -> dict:
    found = [s for s in statuses.values() if s is not None]
    return {
        "requested": len(statuses),
        "statuses": len(found),
        "dnd": sum(s.do_not_disturb for s in found),
        "custom_status": sum(bool(s.custom_status) for s in found),
    }


# op name -> (call(client, args) -> awaitable result, summarize(result) -> dict)
OPS = {
    "spaces.list": (lambda c, a: c.spaces.list(page_size=a["page_size"]), _spaces),
    "messages.list": (
        lambda c, a: c.messages.list(a["space_id"], is_dm=a["is_dm"], page_size=a["page_size"]),
        _messages,
    ),
    "messages.list_thread": (
        lambda c, a: c.messages.list_thread(a["space_id"], a["thread_id"], is_dm=a["is_dm"]),
        _messages,
    ),
    "messages.search": (
        lambda c, a: c.messages.search(a["query"], page_size=a["page_size"]),
        _search,
    ),
    "members.list": (lambda c, a: c.members.list(a["space_id"], is_dm=a["is_dm"]), _users),
    "users.get_status": (lambda c, a: c.users.get_status(a["user_ids"]), _statuses),
}
