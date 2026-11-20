"""Replace personal data in recorded protobuf responses with same-shaped fakes.

Every string field is rewritten; numbers, booleans and enums are kept. The
rewrite is deterministic for one `Scrubber` (same input -> same output), so ids
still line up across responses (a sender id in list_topics matches the one in
get_members). It keeps the shape: digits stay digits, letters stay letters,
punctuation stays, so numeric user ids still look numeric.
"""

import hashlib
import os
import re

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message

# Field names whose values are format metadata, not personal data.
KEEP_FIELDS = frozenset({
    "mimetype", "external_mimetype", "mime_type", "content_type", "unicode", "locale",
})

_TOKEN = re.compile(
    r"(?P<email>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)|(?P<url>https?://[^\s\"'<>|]+)"
)
_LETTERS = "abcdefghijklmnopqrstuvwxyz"


class Scrubber:
    def __init__(self, salt: bytes | None = None):
        # A fresh random salt per recording, never saved: fakes can't be
        # reversed by hashing guesses (e.g. enumerating numeric user ids).
        self._salt = salt if salt is not None else os.urandom(16)
        self.originals: set[str] = set()  # every value replaced, for the leak check

    def _digest(self, s: str, n: int) -> bytes:
        return hashlib.shake_256(self._salt + s.encode()).digest(max(n, 1))

    def _pseudo(self, s: str) -> str:
        stream = self._digest(s, len(s))
        out = []
        for ch, b in zip(s, stream):
            if ch.isdigit():
                out.append(str(b % 10))
            elif ch.isalpha():
                c = _LETTERS[b % 26]
                out.append(c.upper() if ch.isupper() else c)
            else:
                out.append(ch)
        return "".join(out)

    def text(self, s: str) -> str:
        if not s:
            return s
        self.originals.add(s)
        parts, last = [], 0
        for m in _TOKEN.finditer(s):
            parts.append(self._pseudo(s[last:m.start()]))
            tag = self._digest(m.group(), 4).hex()
            parts.append(f"user-{tag}@example.com" if m.group("email")
                         else f"https://example.com/{tag}")
            last = m.end()
        parts.append(self._pseudo(s[last:]))
        return "".join(parts)

    def message(self, msg: Message) -> None:
        """Scrub `msg` in place. Unknown fields (not in our schema) are dropped."""
        msg.DiscardUnknownFields()
        for field, value in msg.ListFields():
            repeated = field.is_repeated
            if field.type == FieldDescriptor.TYPE_MESSAGE:
                for sub in (value if repeated else [value]):
                    self.message(sub)
            elif field.name in KEEP_FIELDS:
                continue
            elif field.type == FieldDescriptor.TYPE_STRING:
                if repeated:
                    new = [self.text(v) for v in value]
                    del value[:]
                    value.extend(new)
                else:
                    setattr(msg, field.name, self.text(value))
            elif field.type == FieldDescriptor.TYPE_BYTES:
                if repeated:
                    del value[:]
                else:
                    msg.ClearField(field.name)


def strings(msg: Message):
    """Every string value in `msg`, nested ones included (after scrubbing: what
    will be written to disk)."""
    for field, value in msg.ListFields():
        values = value if field.is_repeated else [value]
        if field.type == FieldDescriptor.TYPE_MESSAGE:
            for sub in values:
                yield from strings(sub)
        elif field.type == FieldDescriptor.TYPE_STRING:
            yield from values
