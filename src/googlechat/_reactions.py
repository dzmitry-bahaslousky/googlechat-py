from . import _convert
from ._proto import googlechat_pb2 as gc
from ._spaces import SpacesAPI
from ._transport import Transport


class ReactionsAPI:
    def __init__(self, transport: Transport, spaces: SpacesAPI):
        self._t = transport
        self._spaces = spaces

    async def add(self, space_id: str, thread_id: str, message_id: str, emoji: str, *,
                  is_dm: bool | None = None) -> None:
        """React with a unicode emoji, e.g. "👍"."""
        await self._update(space_id, thread_id, message_id, emoji, gc.ADD, is_dm)

    async def remove(self, space_id: str, thread_id: str, message_id: str, emoji: str, *,
                     is_dm: bool | None = None) -> None:
        await self._update(space_id, thread_id, message_id, emoji, gc.REMOVE, is_dm)

    async def _update(self, space_id, thread_id, message_id, emoji, option, is_dm) -> None:
        is_dm = await self._spaces.resolve(space_id, is_dm)
        req = gc.UpdateReactionRequest()
        _convert.fill_message_id(req.message_id, space_id, thread_id, message_id, is_dm)
        req.emoji.unicode = emoji
        req.option = option
        await self._t.call("update_reaction", req, gc.UpdateReactionResponse)
