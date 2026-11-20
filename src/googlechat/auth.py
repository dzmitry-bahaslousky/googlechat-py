"""Saved-cookie loading and XSRF token parsing."""

import json
import os
import re
from pathlib import Path

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
    raw = json.loads(path.read_text())
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


def parse_xsrf_token(html: str) -> str:
    """Pull the XSRF token (WIZ_global_data.SMqcke) out of the /mole/world page.

    Every API call must carry it in X-Framework-XSRF-Token; cookies alone get 400/401.
    """
    m = _WIZ_RE.search(html)
    if not m:
        raise SessionExpiredError("Could not read WIZ_global_data; cookies may be invalid.")
    wiz = json.loads(m.group(1))
    if wiz.get("qwAQke") == "AccountsSignInUi":
        raise SessionExpiredError("Session expired; run `gchat login` to refresh it.")
    token = wiz.get("SMqcke")
    if not token:
        raise GoogleChatError("XSRF token (SMqcke) missing from WIZ_global_data.")
    return token


def save_cookie_updates(path: Path, changes: dict[str, str]) -> None:
    """Write new cookie values into the saved cookie file, keeping its format."""
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


def write_cookie_file(path: Path, data) -> None:
    """Write cookies as JSON, atomically (temp file + rename) and private (0600),
    so a crash or a second process never leaves a half-written file behind."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
