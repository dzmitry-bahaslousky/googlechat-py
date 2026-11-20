"""MCP server: a thin layer that exposes GoogleChatClient as tools."""

import asyncio
import logging
import sys
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from . import config, login
from .client import GoogleChatClient
from .exceptions import SessionExpiredError
from .serialize import status_report, to_json

mcp = FastMCP("googlechat-mcp")
# FastMCP switches root logging to INFO, which makes httpx log every request.
logging.getLogger("httpx").setLevel(logging.WARNING)

_client: GoogleChatClient | None = None


def _get_client() -> GoogleChatClient:
    global _client
    if _client is None:
        _client = GoogleChatClient.from_storage()
    return _client


async def _drop_client() -> None:
    """Forget the client so the next call reloads cookies from disk."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _run(fn, reply: dict | None = None) -> str:
    """Call fn(client) and return its result, or `reply` if given, as JSON text.

    After the session expires, the next call reloads cookies from disk (e.g.
    after `googlechat-mcp login` in a terminal).
    """
    try:
        result = await fn(_get_client())
    except SessionExpiredError:
        await _drop_client()
        raise
    return to_json(result if reply is None else reply)


# ── Read ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_spaces(page_size: int = 20) -> str:
    """List spaces, group chats and DMs, most recently active first."""
    return await _run(lambda c: c.spaces.list(page_size=page_size))


@mcp.tool()
async def read_messages(space_id: str, is_dm: bool | None = None, page_size: int = 20,
                        replies: int = 10) -> str:
    """Read the latest threads of a space or DM: each thread's first message plus its
    newest `replies` replies, with Drive files, attachments and links.

    space_id and is_dm come from list_spaces; in every tool, is_dm may be left out
    and is then looked up. page_size is the number of threads.
    Use read_thread for every message of one thread.
    """
    return await _run(lambda c: c.messages.list(space_id, is_dm=is_dm, page_size=page_size,
                                                replies=replies))


@mcp.tool()
async def read_thread(space_id: str, thread_id: str, is_dm: bool | None = None,
                      limit: int | None = None) -> str:
    """Read every message of one thread (or only its newest `limit`), oldest first."""
    return await _run(lambda c: c.messages.list_thread(space_id, thread_id, is_dm=is_dm,
                                                       limit=limit))


@mcp.tool()
async def search_messages(query: str, space_id: str | None = None, is_dm: bool | None = None,
                          page_size: int = 20, since: str | None = None,
                          until: str | None = None, order: str = "newest") -> str:
    """Search messages server-side across all chats, like Chat's search box.

    space_id (+ is_dm) limits it to one chat. since/until are ISO dates or
    datetimes (e.g. "2026-11-01"; no timezone means UTC). order: "newest" or
    "relevance". Each hit includes its space_id and thread_id.
    """
    def parse(value: str | None) -> datetime | None:
        if not value:
            return None
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return await _run(lambda c: c.messages.search(
        query, space_id=space_id, is_dm=is_dm, page_size=page_size,
        since=parse(since), until=parse(until), order=order))


@mcp.tool()
async def list_space_files(space_id: str, is_dm: bool | None = None, page_size: int = 50) -> str:
    """List files, Drive items and links shared in the latest page_size threads."""
    return await _run(lambda c: c.messages.list_shared(space_id, is_dm=is_dm,
                                                       page_size=page_size))


@mcp.tool()
async def list_members(space_id: str, is_dm: bool | None = None, page_size: int = 50) -> str:
    """List members of a space or DM."""
    return await _run(lambda c: c.members.list(space_id, is_dm=is_dm, page_size=page_size))


@mcp.tool()
async def get_user_status(user_ids: list[str]) -> str:
    """Do-not-disturb state and custom status of users (ids from list_members).

    Users Google returns no status for (e.g. bots) are listed under no_status.
    """
    async def get(c):
        return status_report(await c.users.get_status(user_ids))
    return await _run(get)


# ── Write (posts as the signed-in user) ───────────────────────────────────────

@mcp.tool()
async def send_message(space_id: str, text: str, is_dm: bool | None = None) -> str:
    """Send a message as a new thread in a space or DM. Posts as the user."""
    return await _run(lambda c: c.messages.send(space_id, text, is_dm=is_dm))


@mcp.tool()
async def reply_message(space_id: str, thread_id: str, text: str, is_dm: bool | None = None) -> str:
    """Reply in an existing thread (thread_id from read_messages). Posts as the user."""
    return await _run(lambda c: c.messages.reply(space_id, thread_id, text, is_dm=is_dm))


@mcp.tool()
async def edit_message(space_id: str, thread_id: str, message_id: str, text: str,
                       is_dm: bool | None = None) -> str:
    """Replace the whole text of one of the user's own messages (formatting is not kept)."""
    return await _run(lambda c: c.messages.edit(space_id, thread_id, message_id, text,
                                                is_dm=is_dm))


@mcp.tool()
async def delete_message(space_id: str, thread_id: str, message_id: str,
                         is_dm: bool | None = None) -> str:
    """Delete one of the user's own messages. Deleting the first message of a
    thread (message_id == thread_id) removes it from the thread."""
    return await _run(lambda c: c.messages.delete(space_id, thread_id, message_id, is_dm=is_dm),
                      {"deleted": True, "message_id": message_id})


@mcp.tool()
async def create_dm(user: str) -> str:
    """Open (or get the existing) DM with a user, by Google email or numeric user id."""
    return await _run(lambda c: c.spaces.create_dm(user))


@mcp.tool()
async def add_reaction(space_id: str, thread_id: str, message_id: str, emoji: str,
                       is_dm: bool | None = None) -> str:
    """Add a unicode emoji reaction (e.g. "👍") to a message."""
    return await _run(lambda c: c.reactions.add(space_id, thread_id, message_id, emoji,
                                                is_dm=is_dm),
                      {"added": emoji, "message_id": message_id})


@mcp.tool()
async def remove_reaction(space_id: str, thread_id: str, message_id: str, emoji: str,
                          is_dm: bool | None = None) -> str:
    """Remove a unicode emoji reaction from a message."""
    return await _run(lambda c: c.reactions.remove(space_id, thread_id, message_id, emoji,
                                                   is_dm=is_dm),
                      {"removed": emoji, "message_id": message_id})


@mcp.tool()
async def set_typing(space_id: str, is_typing: bool, thread_id: str = "",
                     is_dm: bool | None = None) -> str:
    """Show or clear the typing indicator (thread_id optional, empty for flat DMs)."""
    return await _run(lambda c: c.spaces.set_typing(space_id, is_typing, thread_id=thread_id,
                                                    is_dm=is_dm),
                      {"is_typing": is_typing})


@mcp.tool()
async def mark_as_read(space_id: str, is_dm: bool | None = None) -> str:
    """Mark a space or DM as read."""
    return await _run(lambda c: c.spaces.mark_read(space_id, is_dm=is_dm),
                      {"marked_read": space_id})


# ── Session ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def sign_in() -> str:
    """Sign in to Google Chat or refresh an expired session. Tries a silent
    refresh from the saved browser profile first; if that fails, opens a Chrome
    window for the user to sign in (waits up to 10 minutes). Call this when
    other tools report the user is not signed in or the session expired."""
    result = await asyncio.to_thread(login.sign_in, config.cookie_file(), config.profile_dir())
    await _drop_client()
    await _get_client().connect()  # validate the new cookies right away
    return to_json(result)


# ── Console script ────────────────────────────────────────────────────────────

async def _check_login() -> None:
    async with GoogleChatClient.from_storage() as client:
        await client.connect()


def cli() -> None:
    """googlechat-mcp [serve|login|logout]"""
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "login":
        print("Refreshing the Google Chat session (a Chrome window opens if you need to sign in)...")
        result = login.sign_in(config.cookie_file(), config.profile_dir())
        asyncio.run(_check_login())
        print(f"Signed in ({result['mode']}); cookies saved to {config.cookie_file()}")
    elif cmd == "logout":
        login.sign_out(config.cookie_file(), config.profile_dir())
        print("Deleted saved cookies and browser profile.")
    elif cmd in ("", "serve"):
        mcp.run()
    else:
        sys.exit(f"Unknown command: {cmd}. Use: googlechat-mcp [serve|login|logout]")


if __name__ == "__main__":
    cli()
