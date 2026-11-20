"""Replay scrubbed live responses and check the library still reads them the same way.

Re-record with `uv run python -m tests.record_fixtures` (needs a signed-in session).
"""

import json
import re

import pytest
from google.protobuf import text_format

from googlechat._proto import googlechat_pb2 as gc

from .conftest import REQUEST_TYPES
from .recorded_ops import FIXTURES, OPS

SCENARIOS = sorted(p.name for p in FIXTURES.iterdir()) if FIXTURES.exists() else []


def _response_cls(method: str):
    return getattr(gc, REQUEST_TYPES[method].__name__.removesuffix("Request") + "Response")


def _key(request) -> bytes:
    request = type(request).FromString(request.SerializeToString())
    request.ClearField("request_header")
    return request.SerializeToString(deterministic=True)


def load(scenario: str) -> tuple[dict, dict[str, list]]:
    """meta.json and, per method, the recorded (request key, response) pairs."""
    d = FIXTURES / scenario
    meta = json.loads((d / "meta.json").read_text())
    pairs: dict[str, list] = {}
    for f in sorted(d.glob("*.request.textproto")):
        method = f.name.split(".")[0].split("_", 1)[1]
        request = text_format.Parse(f.read_text(), REQUEST_TYPES[method]())
        reply = f.with_name(f.name.replace(".request.textproto", ".textproto"))
        response = text_format.Parse(reply.read_text(), _response_cls(method)())
        pairs.setdefault(method, []).append((_key(request), response))
    return meta, pairs


def _answer(pairs: list):
    """Reply with the recorded response for this exact request (calls may run in
    any order, e.g. threads fetched in parallel)."""
    def reply(request):
        key = _key(request)
        for i, (recorded, response) in enumerate(pairs):
            if recorded == key:
                return pairs.pop(i)[1]
        raise AssertionError(f"library sent a request that was not recorded: {request}")
    return reply


@pytest.mark.skipif(not SCENARIOS, reason="no recorded fixtures")
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_replay_matches_live_summary(scenario, client, fake):
    meta, pairs = load(scenario)
    for method, queue in pairs.items():
        fake.replies[method] = _answer(queue)

    call, summarize = OPS[meta["op"]]
    result = await call(client, meta["args"])

    assert summarize(result) == meta["summary"]
    assert all(not q for q in pairs.values()), "library made fewer calls than recorded"


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"https?://([^/\s\"']+)")


@pytest.mark.skipif(not SCENARIOS, reason="no recorded fixtures")
def test_fixtures_hold_no_real_emails_or_links():
    for f in FIXTURES.rglob("*"):
        if f.is_file():
            text = f.read_text()
            assert all(e.endswith("@example.com") for e in _EMAIL.findall(text)), f
            assert all(host == "example.com" for host in _URL.findall(text)), f
