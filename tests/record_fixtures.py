"""Record scrubbed live responses into tests/fixtures/live/ (read-only calls only).

    uv run python -m tests.record_fixtures

Needs a signed-in session (`googlechat-mcp login`). Prints counts only. Refuses
to write anything if an original value or cookie value would end up on disk.
"""

import asyncio
from collections import Counter
import json
import shutil
import sys

from google.protobuf import text_format

from googlechat import GoogleChatClient, config
from googlechat._transport import Transport
from googlechat.auth import load_cookies

from .recorded_ops import FIXTURES, OPS
from .scrub import Scrubber

LEAK_MIN_LEN = 6  # shorter originals ("Team", "ok") collide with ordinary words


def _recording_call(captured: list):
    original = Transport.call

    async def call(self, method, request, response_cls):
        response = await original(self, method, request, response_cls)
        copy = response_cls()
        copy.CopyFrom(response)
        sent = type(request)()
        sent.CopyFrom(request)
        sent.ClearField("request_header")
        captured.append((method, sent, copy))
        return response
    return call


async def _run(op: str, args: dict, captured: list) -> tuple[dict, list]:
    """Run one op with a fresh client (empty caches) and return its summary."""
    call, summarize = OPS[op]
    captured.clear()
    async with GoogleChatClient.from_storage() as client:
        return summarize(await call(client, args)), list(captured)


async def record() -> dict[str, tuple]:
    captured: list = []
    Transport.call = _recording_call(captured)

    async with GoogleChatClient.from_storage() as client:
        spaces = await client.spaces.list(page_size=20)
    room = next(s for s in spaces if not s.is_dm)
    dm = next(s for s in spaces if s.is_dm)
    async with GoogleChatClient.from_storage() as client:
        members = await client.members.list(room.id)
    status_ids = [u.id for u in members if u.id][:3]
    async with GoogleChatClient.from_storage() as client:
        recent = await client.messages.list(room.id, page_size=10, replies=60)
    # The longest of the room's 10 latest threads, so paging gets exercised.
    counts = Counter(m.thread_id for m in recent)
    thread_id = counts.most_common(1)[0][0]

    plan = {
        "spaces_list": ("spaces.list", {"page_size": 20}),
        "space_messages": ("messages.list", {"space_id": room.id, "is_dm": False, "page_size": 10}),
        "dm_messages": ("messages.list", {"space_id": dm.id, "is_dm": True, "page_size": 20}),
        "space_members": ("members.list", {"space_id": room.id, "is_dm": False}),
        "user_status": ("users.get_status", {"user_ids": status_ids}),
        "space_thread": ("messages.list_thread",
                         {"space_id": room.id, "thread_id": thread_id, "is_dm": False}),
        "search": ("messages.search", {"query": "release", "page_size": 20}),
    }
    out = {}
    for name, (op, args) in plan.items():
        summary, responses = await _run(op, args, captured)
        out[name] = (op, args, summary, responses)
    return out


def scrub_and_check(recorded: dict) -> dict[str, dict[str, str]]:
    """Scrub everything, then verify no original or cookie value survives."""
    scrubber = Scrubber()
    files: dict[str, dict[str, str]] = {}
    for name, (op, args, summary, responses) in recorded.items():
        scrubbed_args = {
            k: (scrubber.text(v) if isinstance(v, str)
                else [scrubber.text(x) for x in v] if isinstance(v, list) else v)
            for k, v in args.items()
        }
        scenario = {"meta.json": json.dumps(
            {"op": op, "args": scrubbed_args, "summary": summary},
            indent=2, ensure_ascii=False) + "\n"}
        for i, (method, request, response) in enumerate(responses):
            scrubber.message(request)
            scrubber.message(response)
            scenario[f"{i:02d}_{method}.request.textproto"] = text_format.MessageToString(
                request, as_utf8=True)
            scenario[f"{i:02d}_{method}.textproto"] = text_format.MessageToString(
                response, as_utf8=True)
        files[name] = scenario

    blob = "\n".join(t for scenario in files.values() for t in scenario.values())
    secrets = {v for v in load_cookies(config.cookie_file()).values() if len(v) >= 8}
    leaks = [s for s in scrubber.originals if len(s) >= LEAK_MIN_LEN and s in blob]
    leaks += [s for s in secrets if s in blob]
    if leaks:
        sys.exit(f"Refusing to write: {len(leaks)} original values survived scrubbing.")
    return files


def main() -> None:
    files = scrub_and_check(asyncio.run(record()))
    shutil.rmtree(FIXTURES, ignore_errors=True)
    for name, scenario in files.items():
        d = FIXTURES / name
        d.mkdir(parents=True)
        for fname, text in scenario.items():
            (d / fname).write_text(text)
        summary = json.loads(scenario["meta.json"])["summary"]
        print(f"{name}: {(len(scenario) - 1) // 2} responses, summary={summary}")


if __name__ == "__main__":
    main()
