"""Saved-cookie loading and XSRF token parsing."""

import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no file locks; concurrent saves may drop an update
    fcntl = None

from .exceptions import GoogleChatError, NotSignedInError, SessionExpiredError

# Google's web session needs the SID-family cookies. Without SID,
# chat.google.com serves the signed-out UI and every API call returns 401.
REQUIRED_COOKIES = frozenset({"SAPISID", "SID", "__Secure-1PSID"})


def load_cookies(path: Path) -> dict[str, str]:
    """Read cookies saved by `login` or exported with a cookie-editor extension.

    Accepts a list of {"name"/"key", "value", ...} objects or a flat name -> value map.
    """
    if not path.exists():
        raise NotSignedInError(
            f"Not signed in: no cookies at {path}. Run `gchat login` "
            "(or the MCP sign_in tool) to open Chrome and sign in."
        )
    try:
        raw = json.loads(path.read_text())
    except ValueError:
        raise NotSignedInError(
            f"Cookie file at {path} is not valid JSON. Run `gchat login` to replace it."
        ) from None
    if isinstance(raw, list):
        cookies = {
            c.get("name") or c.get("key", ""): c.get("value", "")
            for c in raw
        }
        cookies.pop("", None)
    elif isinstance(raw, dict):
        cookies = dict(raw)
    else:
        raise NotSignedInError(f"Unrecognised cookie file format at {path}.")

    missing = REQUIRED_COOKIES - cookies.keys()
    if missing:
        raise NotSignedInError(
            f"Incomplete cookies. Missing: {sorted(missing)}. Export the FULL "
            "cookie set from chat.google.com while signed in (SID, HSID and "
            "APISID must be included), or run `gchat login`."
        )
    return cookies


_WIZ_RE = re.compile(r"window\.WIZ_global_data = (.*?);</script>", re.S)


def parse_tokens(html: str) -> tuple[str, str | None]:
    """Pull the two request tokens out of the /mole/world page's WIZ_global_data.

    SMqcke: every /api/ call carries it in X-Framework-XSRF-Token; cookies alone
    get 400/401. SNlM0e: batchexecute calls send it as the `at` form field
    (without it: 400). None if the page has no SNlM0e.
    """
    m = _WIZ_RE.search(html)
    if not m:
        raise SessionExpiredError("Could not read WIZ_global_data; cookies may be invalid.")
    try:
        wiz = json.loads(m.group(1))
    except ValueError:
        raise GoogleChatError("Could not parse WIZ_global_data; Google changed the page?") from None
    if wiz.get("qwAQke") == "AccountsSignInUi":
        raise SessionExpiredError("Session expired; run `gchat login` to refresh it.")
    token = wiz.get("SMqcke")
    if not token:
        raise GoogleChatError("XSRF token (SMqcke) missing from WIZ_global_data.")
    return token, wiz.get("SNlM0e") or None


def save_cookie_updates(path: Path, changes: dict[str, str]) -> None:
    """Write new cookie values into the saved cookie file, keeping its format.

    Locked, so two processes (e.g. the CLI and the MCP server) saving at the
    same time don't drop each other's updates.
    """
    with _locked(path):
        raw = json.loads(path.read_text())
        if isinstance(raw, list):
            known = set()
            for c in raw:
                name = c.get("name") or c.get("key")
                if name in changes:
                    c["value"] = changes[name]
                    known.add(name)
            raw.extend({"name": n, "value": v, "domain": ".google.com", "path": "/"}
                       for n, v in changes.items() if n not in known)
        else:
            raw.update(changes)
        write_cookie_file(path, raw)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on a sidecar file next to `path`."""
    if fcntl is None:
        yield
        return
    fd = os.open(path.with_name(f".{path.name}.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # released when the file is closed
        yield


def write_cookie_file(path: Path, data) -> None:
    """Write cookies as JSON, atomically (temp file + rename) and private (0600),
    so a crash or a second process never leaves a half-written file behind."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())  # data on disk before the rename, or a crash can leave it empty
    os.replace(tmp, path)
