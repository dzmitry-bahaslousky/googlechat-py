from collections.abc import Callable
from pathlib import Path

import httpx

from . import config
from ._messages import MessagesAPI
from ._reactions import ReactionsAPI
from ._spaces import MembersAPI, SpacesAPI
from ._transport import Transport
from ._users import UsersAPI
from .auth import load_cookies, save_cookie_updates


class GoogleChatClient:
    """Async client for Google Chat's private web API, signed in as you.

        async with GoogleChatClient.from_storage() as chat:
            for space in await chat.spaces.list():
                print(space.name)
    """

    def __init__(self, cookies: dict[str, str], *, http: httpx.AsyncClient | None = None,
                 on_cookies_changed: Callable[[dict[str, str]], None] | None = None):
        """`on_cookies_changed` receives cookies Google replaced (e.g. to save them)."""
        self._transport = Transport(cookies, http=http, on_cookies_changed=on_cookies_changed)
        self.users = UsersAPI(self._transport)
        self.spaces = SpacesAPI(self._transport)
        self.members = MembersAPI(self._transport, self.users, self.spaces)
        self.messages = MessagesAPI(self._transport, self.users, self.spaces)
        self.reactions = ReactionsAPI(self._transport, self.spaces)

    @classmethod
    def from_storage(cls, cookie_file: Path | None = None, *,
                     http: httpx.AsyncClient | None = None) -> "GoogleChatClient":
        """Build a client from the cookies saved by `login` (or GCHAT_COOKIES).

        Cookies Google replaces during the session are written back to that file.
        """
        path = cookie_file or config.cookie_file()
        return cls(load_cookies(path), http=http,
                   on_cookies_changed=lambda changes: save_cookie_updates(path, changes))

    async def connect(self) -> None:
        """Check the session now (fetches the XSRF token)."""
        await self._transport.connect()

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def __aenter__(self) -> "GoogleChatClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()
