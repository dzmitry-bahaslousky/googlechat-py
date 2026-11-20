"""Capture Google Chat session cookies with a real Chrome via Playwright.

A dedicated Chrome profile keeps the Google login, so later refreshes can run
headless with no window. Cookie values are written to a 0600 file and never
printed or logged.
"""
import shutil
import time
from pathlib import Path

from . import config
from .auth import REQUIRED_COOKIES, write_cookie_file
from .exceptions import GoogleChatError

CHAT_URL = "https://chat.google.com/"
# Left in a profile folder that login created, so sign_out deletes only folders
# it owns, never e.g. a real Chrome profile GCHAT_PROFILE was pointed at.
PROFILE_MARKER = ".googlechat-profile"


class LoginError(GoogleChatError):
    """Signing in failed: timed out, window closed, or Chrome could not run."""


def _signed_in(ctx, page) -> bool:
    try:
        url = page.url
    except Exception:
        return False
    names = {c["name"] for c in ctx.cookies([CHAT_URL])}
    return url.startswith("https://chat.google.com") and REQUIRED_COOKIES <= names


def capture_cookies(out_path: Path, profile_dir: Path, *, headless: bool, timeout: int) -> list[str]:
    """Open Chat in the given profile, wait for a signed-in session, save cookies.

    Returns the saved cookie names. Raises LoginError on timeout, if the
    window is closed before sign-in completes, or if Chrome cannot run.
    """
    from playwright.sync_api import Error as PlaywrightError  # heavy import, only when needed

    new_profile = not profile_dir.exists() or not any(profile_dir.iterdir())
    profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if new_profile:
        (profile_dir / PROFILE_MARKER).touch()
    out_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        cookies = _read_cookies(profile_dir, headless=headless, timeout=timeout)
    except PlaywrightError as e:
        detail = (str(e).strip().splitlines() or ["no details"])[0]
        raise LoginError(
            f"Chrome could not be started or controlled ({detail}). "
            "Is Google Chrome installed, and is no other sign-in window open?"
        ) from None
    write_cookie_file(out_path, cookies)
    return sorted(c["name"] for c in cookies)


def _read_cookies(profile_dir: Path, *, headless: bool, timeout: int) -> list[dict]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir),
            channel="chrome",
            headless=headless,
            # Playwright turns Chrome's sandbox off by default (--no-sandbox);
            # this window holds a Google login, so keep it on.
            chromium_sandbox=True,
            no_viewport=True,
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(CHAT_URL)
            deadline = time.time() + timeout
            while not _signed_in(ctx, page):
                if time.time() > deadline:
                    raise LoginError("Timed out waiting for Google Chat sign-in.")
                if page.is_closed():
                    raise LoginError("Browser window was closed before sign-in finished.")
                time.sleep(2)
            time.sleep(5)  # let Chat finish loading so late cookies (COMPASS, OSID) are set
            cookies = [
                {"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c["path"]}
                for c in ctx.cookies([CHAT_URL])
            ]
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return cookies


def sign_in(out_path: Path, profile_dir: Path, *, interactive_timeout: int = 600) -> dict:
    """Refresh cookies silently if the saved profile is still logged in,
    otherwise open a visible Chrome window for the user to sign in."""
    try:
        names = capture_cookies(out_path, profile_dir, headless=True, timeout=25)
        return {"signed_in": True, "mode": "headless refresh", "cookies": len(names)}
    except LoginError:
        pass
    names = capture_cookies(out_path, profile_dir, headless=False, timeout=interactive_timeout)
    return {"signed_in": True, "mode": "interactive", "cookies": len(names)}


def sign_out(out_path: Path, profile_dir: Path) -> bool:
    """Delete the saved cookies and the browser profile that keeps the login.

    The profile is deleted only if login created it (or it is the default
    location); returns False if it was left in place.
    """
    out_path.unlink(missing_ok=True)
    owned = ((profile_dir / PROFILE_MARKER).exists()
             or profile_dir.resolve() == config.default_profile_dir().resolve())
    if owned:
        shutil.rmtree(profile_dir, ignore_errors=True)
    return owned or not profile_dir.exists()
