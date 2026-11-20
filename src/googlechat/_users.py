import logging
from collections.abc import Iterable

import httpx

from . import _convert
from ._proto import googlechat_pb2 as gc
from ._transport import Transport
from .exceptions import GoogleChatError
from .types import User, UserStatus

logger = logging.getLogger(__name__)


class UsersAPI:
    def __init__(self, transport: Transport):
        self._t = transport
        self._cache: dict[str, User] = {}  # names rarely change; cache per client

    async def get(self, user_ids: Iterable[str]) -> dict[str, User]:
        """Look up profiles (name, email) by user id. Unknown ids are left out."""
        ids = [u for u in dict.fromkeys(user_ids) if u]
        missing = [u for u in ids if u not in self._cache]
        if missing:
            req = gc.GetMembersRequest()
            for uid in missing:
                req.member_ids.add().user_id.id = uid
            resp = await self._t.call("get_members", req, gc.GetMembersResponse)
            for member in resp.members:
                if member.user.user_id.id:
                    self._cache[member.user.user_id.id] = _convert.user(member.user)
        return {u: self._cache[u] for u in ids if u in self._cache}

    async def fill_names(self, users: Iterable[User]) -> None:
        """Best-effort: fill name/email on users that only carry an id."""
        users = [u for u in users if u.id and not u.name]
        if not users:
            return
        try:
            found = await self.get(u.id for u in users)
        except (GoogleChatError, httpx.HTTPError) as e:  # names are nice-to-have; never fail a read
            logger.warning("get_members failed: %s", e)
            return
        for u in users:
            info = found.get(u.id)
            if info:
                u.name = info.name
                u.email = u.email or info.email

    async def get_status(self, user_ids: Iterable[str]) -> dict[str, UserStatus | None]:
        """Do-not-disturb state and custom status, keyed by every requested id.

        None means Google returned no status for that user (e.g. bots, or
        users who don't share their status).
        """
        ids = list(dict.fromkeys(user_ids))
        req = gc.GetUserStatusRequest()
        for uid in ids:
            req.user_ids.add().id = uid
        resp = await self._t.call("get_user_status", req, gc.GetUserStatusResponse)
        out: dict[str, UserStatus | None] = dict.fromkeys(ids)
        for s in resp.user_statuses:
            cs = s.custom_status if s.HasField("custom_status") else None
            out[s.user_id.id] = UserStatus(
                user_id=s.user_id.id,
                do_not_disturb=s.dnd_settings.dnd_state == gc.DndSettings.DND,
                custom_status=(cs.status_text or None) if cs else None,
                custom_emoji=(cs.status_emoji or None) if cs else None,
            )
        return out
