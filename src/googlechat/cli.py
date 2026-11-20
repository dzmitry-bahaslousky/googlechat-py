"""`gchat`: command-line access to Google Chat, a thin layer over GoogleChatClient."""

import asyncio
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import click

from . import config, login as login_flow
from .client import GoogleChatClient
from .exceptions import GoogleChatError
from .serialize import status_report, to_json
from .types import Attachment, DriveFile, Link, Message, SharedItem, Space


def make_client() -> GoogleChatClient:
    return GoogleChatClient.from_storage()


def _interactive() -> bool:
    return sys.stdin.isatty()


def run(fn: Callable[[GoogleChatClient], Awaitable[Any]]) -> Any:
    """Run fn(client) on a fresh client; library errors become CLI errors."""
    async def go():
        async with make_client() as client:
            return await fn(client)
    try:
        return asyncio.run(go())
    except GoogleChatError as e:
        raise click.ClickException(str(e)) from e


def emit(ctx: click.Context, data: Any, as_text: Callable[[Any], None]) -> None:
    if ctx.obj["json"]:
        click.echo(to_json(data))
    else:
        as_text(data)


def _time(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else "?"


def _local(day: datetime | None) -> datetime | None:
    """click gives naive datetimes; treat them as local time."""
    return day.astimezone() if day else None


def _text_arg(text: str) -> str:
    """`-` means: read the text from stdin."""
    return sys.stdin.read().rstrip("\n") if text == "-" else text


# ── Space lookup and write confirmation ──────────────────────────────────────

space_kind = click.option(
    "--dm/--space", "is_dm", default=None,
    help="Say whether SPACE is a DM or a named space. Omitted: looked up in your chat list.",
)
yes_option = click.option("--yes", "-y", is_flag=True, help="Don't ask for confirmation.")


async def resolve(client: GoogleChatClient, space_id: str, is_dm: bool | None) -> Space:
    if is_dm is not None:
        return Space(id=space_id, is_dm=is_dm)
    found = await client.spaces.find(space_id)
    if found is None:
        raise click.ClickException(
            f"{space_id} is not in your chat list; pass --dm or --space to use it anyway.")
    return found


def _label(space: Space) -> str:
    return f"{'DM' if space.is_dm else 'space'} {space.name or space.id}"


def confirm(yes: bool, question: str) -> None:
    """Writes post as you: ask first, and never write silently from a script."""
    if yes:
        return
    if not _interactive():
        raise click.ClickException("Not an interactive terminal: pass --yes to confirm.")
    click.confirm(question, abort=True)


def run_in(space: str, is_dm: bool | None,
           fn: Callable[[GoogleChatClient, Space], Awaitable[Any]], *,
           ask: Callable[[Space], str] | None = None, yes: bool = False) -> Any:
    """Resolve SPACE, confirm with `ask(space)` if given (writes), then run fn(client, space)."""
    async def go(c: GoogleChatClient):
        s = await resolve(c, space, is_dm)
        if ask:
            confirm(yes, ask(s))
        return await fn(c, s)
    return run(go)


# ── Text output ───────────────────────────────────────────────────────────────

def _item_line(item: DriveFile | Attachment | Link) -> str:
    if isinstance(item, DriveFile):
        return f"drive: {item.title or item.drive_id} ({item.kind})"
    if isinstance(item, Attachment):
        return f"file: {item.filename or '?'}"
    return f"link: {item.url or item.title or '?'}"


def print_messages(messages: list[Message], *, by_thread: bool = True) -> None:
    """Grouped under `thread <id>` lines, or (search) one block per message with
    where it was posted."""
    thread = None
    for m in messages:
        if by_thread and m.thread_id != thread:
            thread = m.thread_id
            click.echo(f"\nthread {thread}")
        who = m.sender.name or m.sender.email or m.sender.id or "?"
        if not by_thread:
            where = f"{'dm' if m.is_dm else 'space'} {m.space_id}  thread {m.thread_id}"
            click.echo(f"\n{where}")
        click.echo(f"  {_time(m.create_time)}  {who}  [{m.id}]")
        for line in (m.text or "").splitlines():
            click.echo(f"    {line}")
        for item in [*m.drive_files, *m.attachments, *m.links]:
            click.echo(f"    {_item_line(item)}")


# ── Commands ──────────────────────────────────────────────────────────────────

@click.group()
@click.option("--json", "as_json", is_flag=True, help="Print JSON instead of text.")
@click.pass_context
def main(ctx: click.Context, as_json: bool) -> None:
    """Google Chat from the command line, signed in as you.

    SPACE, THREAD and MESSAGE are ids: get them from `gchat spaces` and `gchat read`.
    """
    ctx.obj = {"json": as_json}


@main.command()
def login() -> None:
    """Sign in (silent refresh first; opens Chrome if needed)."""
    click.echo("Refreshing the session (a Chrome window opens if you need to sign in)...")
    result = login_flow.sign_in(config.cookie_file(), config.profile_dir())
    run(lambda c: c.connect())
    click.echo(f"Signed in ({result['mode']}); cookies saved to {config.cookie_file()}")


@main.command()
def logout() -> None:
    """Delete the saved cookies and browser profile."""
    login_flow.sign_out(config.cookie_file(), config.profile_dir())
    click.echo("Deleted saved cookies and browser profile.")


@main.command()
def check() -> None:
    """Check that the saved session still works."""
    run(lambda c: c.connect())
    click.echo("Session OK.")


@main.command()
@click.option("--limit", "-n", default=20, show_default=True, help="How many chats.")
@click.pass_context
def spaces(ctx: click.Context, limit: int) -> None:
    """List spaces, group chats and DMs, most recently active first."""
    result = run(lambda c: c.spaces.list(page_size=limit))

    def text(items: list[Space]) -> None:
        for s in items:
            click.echo(f"{s.id}  {'dm   ' if s.is_dm else 'space'}  {s.name or '(unnamed)'}")
    emit(ctx, result, text)


@main.command()
@click.argument("space")
@space_kind
@click.option("--threads", "-n", default=10, show_default=True, help="How many recent threads.")
@click.option("--replies", "-r", default=10, show_default=True,
              help="Newest replies shown per thread (plus its first message).")
@click.pass_context
def read(ctx: click.Context, space: str, is_dm: bool | None, threads: int, replies: int) -> None:
    """Read the latest threads of SPACE."""
    emit(ctx, run_in(space, is_dm, lambda c, s: c.messages.list(
        s.id, is_dm=s.is_dm, page_size=threads, replies=replies)), print_messages)


@main.command()
@click.argument("space")
@click.argument("thread")
@space_kind
@click.option("--limit", "-n", type=int, default=None, help="Only the newest N messages.")
@click.pass_context
def thread(ctx: click.Context, space: str, thread: str, is_dm: bool | None,
           limit: int | None) -> None:
    """Read every message of THREAD in SPACE."""
    emit(ctx, run_in(space, is_dm, lambda c, s: c.messages.list_thread(
        s.id, thread, is_dm=s.is_dm, limit=limit)), print_messages)


@main.command()
@click.argument("query")
@click.option("--in", "space", default=None, help="Only search this space or DM.")
@space_kind
@click.option("--limit", "-n", default=20, show_default=True, help="How many results.")
@click.option("--since", type=click.DateTime(["%Y-%m-%d"]), default=None,
              help="Only messages from this day on (YYYY-MM-DD, local time).")
@click.option("--until", type=click.DateTime(["%Y-%m-%d"]), default=None,
              help="Only messages before this day (YYYY-MM-DD, local time).")
@click.option("--relevance", is_flag=True, help="Best matches first instead of newest first.")
@click.pass_context
def search(ctx: click.Context, query: str, space: str | None, is_dm: bool | None, limit: int,
           since: datetime | None, until: datetime | None, relevance: bool) -> None:
    """Search your chats for QUERY (server-side, like the search box in Chat)."""
    async def go(c):
        s = await resolve(c, space, is_dm) if space else None
        return await c.messages.search(
            query, space_id=s.id if s else None, is_dm=s.is_dm if s else False,
            page_size=limit, since=_local(since), until=_local(until),
            order="relevance" if relevance else "newest")
    emit(ctx, run(go), lambda msgs: print_messages(msgs, by_thread=False))


@main.command()
@click.argument("space")
@space_kind
@click.option("--threads", "-n", default=50, show_default=True, help="How many recent threads.")
@click.pass_context
def files(ctx: click.Context, space: str, is_dm: bool | None, threads: int) -> None:
    """List Drive files, uploads and links shared in SPACE."""
    result = run_in(space, is_dm, lambda c, s: c.messages.list_shared(
        s.id, is_dm=s.is_dm, page_size=threads))

    def text(items: list[SharedItem]) -> None:
        for i in items:
            who = i.sender.name or i.sender.id or "?"
            click.echo(f"{_time(i.create_time)}  {who}  {_item_line(i.item)}")
    emit(ctx, result, text)


@main.command()
@click.argument("space")
@space_kind
@click.pass_context
def members(ctx: click.Context, space: str, is_dm: bool | None) -> None:
    """List members of SPACE."""
    result = run_in(space, is_dm, lambda c, s: c.members.list(s.id, is_dm=s.is_dm, page_size=500))

    def text(users) -> None:
        for u in users:
            bot = "  (bot)" if u.is_bot else ""
            click.echo(f"{u.id}  {u.name or '?'}  {u.email or ''}{bot}".rstrip())
    emit(ctx, result, text)


@main.command()
@click.argument("user_ids", nargs=-1, required=True)
@click.pass_context
def status(ctx: click.Context, user_ids: tuple[str, ...]) -> None:
    """Show do-not-disturb state and custom status of USER_IDS."""
    result = run(lambda c: c.users.get_status(user_ids))

    def text(statuses: dict) -> None:
        for uid, s in statuses.items():
            if s is None:
                click.echo(f"{uid}  (no status)")
                continue
            state = "do not disturb" if s.do_not_disturb else "available"
            custom = " ".join(x for x in (s.custom_emoji, s.custom_status) if x)
            click.echo(f"{uid}  {state}  {custom}".rstrip())
    emit(ctx, status_report(result) if ctx.obj["json"] else result, text)


@main.command()
@click.argument("user")
@click.pass_context
def dm(ctx: click.Context, user: str) -> None:
    """Open (or get the existing) DM with USER (email or numeric user id)."""
    emit(ctx, run(lambda c: c.spaces.create_dm(user)), lambda s: click.echo(s.id))


@main.command("mark-read")
@click.argument("space")
@space_kind
def mark_read(space: str, is_dm: bool | None) -> None:
    """Mark SPACE as read."""
    run_in(space, is_dm, lambda c, s: c.spaces.mark_read(s.id, is_dm=s.is_dm))
    click.echo("Marked as read.")


# ── Writes (post as you; ask first unless --yes) ─────────────────────────────

@main.command()
@click.argument("space")
@click.argument("text")
@space_kind
@yes_option
@click.pass_context
def send(ctx: click.Context, space: str, text: str, is_dm: bool | None, yes: bool) -> None:
    """Post TEXT as a new thread in SPACE (TEXT `-` reads stdin)."""
    text = _text_arg(text)

    result = run_in(space, is_dm, lambda c, s: c.messages.send(s.id, text, is_dm=s.is_dm),
                    ask=lambda s: f"Send to {_label(s)}:\n{text}\n", yes=yes)
    emit(ctx, result, lambda r: click.echo(f"Sent: thread {r.thread_id} message {r.id}"))


@main.command()
@click.argument("space")
@click.argument("thread")
@click.argument("text")
@space_kind
@yes_option
@click.pass_context
def reply(ctx: click.Context, space: str, thread: str, text: str, is_dm: bool | None,
          yes: bool) -> None:
    """Post TEXT as a reply in THREAD of SPACE (TEXT `-` reads stdin)."""
    text = _text_arg(text)

    result = run_in(space, is_dm, lambda c, s: c.messages.reply(s.id, thread, text, is_dm=s.is_dm),
                    ask=lambda s: f"Reply in {_label(s)}, thread {thread}:\n{text}\n", yes=yes)
    emit(ctx, result, lambda r: click.echo(f"Sent: thread {r.thread_id} message {r.id}"))


@main.command()
@click.argument("space")
@click.argument("thread")
@click.argument("message")
@click.argument("text")
@space_kind
@yes_option
@click.pass_context
def edit(ctx: click.Context, space: str, thread: str, message: str, text: str,
         is_dm: bool | None, yes: bool) -> None:
    """Replace the text of your MESSAGE (TEXT `-` reads stdin)."""
    text = _text_arg(text)

    result = run_in(
        space, is_dm, lambda c, s: c.messages.edit(s.id, thread, message, text, is_dm=s.is_dm),
        ask=lambda s: f"Edit message {message} in {_label(s)} to:\n{text}\n", yes=yes)
    emit(ctx, result, lambda r: click.echo(f"Edited message {r.id}"))


@main.command()
@click.argument("space")
@click.argument("thread")
@click.argument("message")
@space_kind
@yes_option
@click.pass_context
def delete(ctx: click.Context, space: str, thread: str, message: str, is_dm: bool | None,
           yes: bool) -> None:
    """Delete your MESSAGE."""
    run_in(space, is_dm, lambda c, s: c.messages.delete(s.id, thread, message, is_dm=s.is_dm),
           ask=lambda s: f"Delete message {message} in {_label(s)}", yes=yes)
    emit(ctx, {"deleted": message}, lambda _: click.echo(f"Deleted message {message}"))


@main.command()
@click.argument("space")
@click.argument("thread")
@click.argument("message")
@click.argument("emoji")
@click.option("--remove", is_flag=True, help="Remove the reaction instead of adding it.")
@space_kind
@yes_option
def react(space: str, thread: str, message: str, emoji: str, remove: bool,
          is_dm: bool | None, yes: bool) -> None:
    """Add (or --remove) an EMOJI reaction on MESSAGE."""
    verb = "Remove" if remove else "Add"
    run_in(space, is_dm,
           lambda c, s: (c.reactions.remove if remove else c.reactions.add)(
               s.id, thread, message, emoji, is_dm=s.is_dm),
           ask=lambda s: f"{verb} {emoji} on message {message} in {_label(s)}", yes=yes)
    click.echo(f"{'Removed' if remove else 'Added'} {emoji}")


if __name__ == "__main__":
    main()
