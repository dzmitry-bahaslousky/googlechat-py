from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import AsyncIterator

from . import _convert
from ._proto import googlechat_pb2 as gc
from ._transport import Transport
from ._users import UsersAPI
from .exceptions import GoogleChatError, SpaceNotFoundError
from .types import Section, Space, User


class SpacesAPI:
    def __init__(self, transport: Transport):
        self._t = transport
        self._is_dm: dict[str, bool] = {}  # never changes; learned from every listing

    async def resolve(self, space_id: str, is_dm: bool | None) -> bool:
        """`is_dm` if given; otherwise whether `space_id` is a DM, looked up in
        your chat list (once per client)."""
        if is_dm is not None:
            return is_dm
        if space_id not in self._is_dm:
            await self.find(space_id)
        if space_id not in self._is_dm:
            raise SpaceNotFoundError(
                f"{space_id} is not in your chat list; pass is_dm to use it anyway.")
        return self._is_dm[space_id]

    async def iterate(self, *, batch_size: int = 200) -> AsyncIterator[Space]:
        """Every space, group chat and DM, most recently active first, page by page."""
        token = ""
        while True:
            req = gc.PaginatedWorldRequest()
            req.fetch_from_user_spaces = True
            req.fetch_snippets_for_unnamed_rooms = True
            section = req.world_section_requests.add()
            section.page_size = batch_size  # without a page size the world comes back empty
            if token:
                section.pagination_token = token
            resp = await self._t.call("paginated_world", req, gc.PaginatedWorldResponse)
            # Pages come in recency order, but items inside a large page do not.
            for item in sorted(resp.world_items, key=lambda i: -i.sort_timestamp):
                space = _convert.space(item)
                self._is_dm[space.id] = space.is_dm
                yield space
            sec = resp.world_section_responses[0] if resp.world_section_responses else None
            if not (sec and sec.has_more_items and sec.pagination_token):
                return
            token = sec.pagination_token

    async def list(self, *, page_size: int = 20) -> list[Space]:
        """The `page_size` most recently active spaces, group chats and DMs."""
        out: list[Space] = []
        if page_size <= 0:
            return out
        async for space in self.iterate(batch_size=min(page_size, 500)):
            out.append(space)
            if len(out) == page_size:
                break
        return out

    async def sections(self, name: str | None = None) -> list[Section]:
        """Your sidebar sections in sidebar order, each with its chats.

        With `name`, only the section of that name (any case) or id.
        Reads your whole chat list to fill them.
        """
        req = gc.PaginatedWorldRequest(fetch_from_user_spaces=True)
        req.world_section_requests.add(page_size=1, fetch_roster_sections=True)
        resp = await self._t.call("paginated_world", req, gc.PaginatedWorldResponse)
        infos = [r.info for sec in resp.world_section_responses for r in sec.roster_sections
                 if r.info.section_id.group_label_id.group_label_type
                 == gc.GroupLabelId.CUSTOM_SECTION]  # the rest are built-in (DMs, spaces, apps)
        out = [Section(id=i.section_id.group_label_id.label_secondary_key, name=i.name)
               for i in sorted(infos, key=lambda i: i.sort_order)]
        if name is not None:
            out = [s for s in out if name.casefold() in (s.name.casefold(), s.id)]
        if out:
            by_id = {s.id: s for s in out}
            async for space in self.iterate(batch_size=500):
                if space.section_id in by_id:
                    by_id[space.section_id].spaces.append(space)
        return out

    async def find(self, space_id: str) -> Space | None:
        """Look a space or DM up by id in your chat list (None if absent)."""
        async for space in self.iterate(batch_size=500):
            if space.id == space_id:
                return space
        return None

    async def create_dm(self, user: str) -> Space:
        """Open (or get the existing) DM with a user, by email or numeric user id.

        Uses create_dm_extended, as the web client does: for someone you already
        have a DM with, Google returns that DM instead of creating one. A DM
        with yourself is not possible.
        """
        req = gc.CreateDmExtendedRequest(flag_8=False, space_origin=12)
        invitee = req.members.add()
        if user.isdigit():
            invitee.user_id.id = user
            invitee.invitation_mode = 1  # GAIA_ID
        else:
            invitee.email = user
            invitee.invitation_mode = 2  # EMAIL_ONLY
        req.retention_settings.state = gc.RetentionSettings.PERMANENT  # history on
        resp = await self._t.call("create_dm_extended", req, gc.CreateDmResponse)
        dm_id = resp.dm.group_id.dm_id.dm_id
        if not dm_id:
            raise GoogleChatError("create_dm_extended returned no DM id.")
        self._is_dm[dm_id] = True
        return Space(id=dm_id, is_dm=True)

    async def mark_read(self, space_id: str, *, is_dm: bool | None = None) -> None:
        is_dm = await self.resolve(space_id, is_dm)
        req = gc.MarkGroupReadstateRequest()
        req.id.CopyFrom(_convert.group_id(space_id, is_dm))
        req.last_read_time = _convert.to_micros(datetime.now(timezone.utc))
        await self._t.call("mark_group_readstate", req, gc.MarkGroupReadstateResponse)

    async def set_typing(self, space_id: str, typing: bool, *, thread_id: str = "",
                         is_dm: bool | None = None) -> None:
        """Show or clear your typing indicator."""
        is_dm = await self.resolve(space_id, is_dm)
        req = gc.SetTypingStateRequest()
        req.state = gc.TYPING if typing else gc.STOPPED
        req.context.group_id.CopyFrom(_convert.group_id(space_id, is_dm))
        if thread_id:
            req.context.topic_id.CopyFrom(_convert.topic_id(space_id, thread_id, is_dm))
        await self._t.call("set_typing_state", req, gc.SetTypingStateResponse)


class MembersAPI:
    def __init__(self, transport: Transport, users: UsersAPI, spaces: SpacesAPI):
        self._t = transport
        self._users = users
        self._spaces = spaces

    async def list(self, space_id: str, *, is_dm: bool | None = None,
                   page_size: int = 50) -> list[User]:
        is_dm = await self._spaces.resolve(space_id, is_dm)
        req = gc.ListMembersRequest()
        req.page_size = page_size
        # The API wants group_id (field 5) for both rooms and DMs; the legacy
        # space_id field (1) is rejected with 400.
        req.group_id.CopyFrom(_convert.group_id(space_id, is_dm))
        resp = await self._t.call("list_members", req, gc.ListMembersResponse)

        out = [_convert.user(m.user) for m in resp.members]
        # Usually only memberships come back (ids without profiles); resolve names.
        seen = {u.id for u in out if u.id}
        bare = [User(id=uid) for uid in dict.fromkeys(
            ms.id.member_id.user_id.id for ms in resp.memberships
        ) if uid and uid not in seen]
        await self._users.fill_names(bare)
        return out + bare
