"""Errors raised by the Google Chat client."""


class GoogleChatError(Exception):
    """Base class for every error this library raises."""


class NotSignedInError(GoogleChatError):
    """No usable saved session: cookies are missing or incomplete."""


class SessionExpiredError(GoogleChatError):
    """Google rejected the saved session; sign in again."""


class SpaceNotFoundError(GoogleChatError):
    """A space id that is not in your chat list, so whether it is a DM is unknown."""


class MembersNotAddedError(GoogleChatError):
    """Some people were not added to a space. If `space_created`, the space
    itself exists (id `space_id`): add them with `members.add`, don't create it again."""

    def __init__(self, space_id: str, missing: list[str], reason: str, *,
                 space_created: bool = False):
        who = ", ".join(missing)
        if space_created:
            text = (f"Space {space_id} was created, but these people were not added: {who} "
                    f"({reason}). Do not create the space again; add them to space {space_id}.")
        else:
            text = f"These people were not added to space {space_id}: {who} ({reason})."
        super().__init__(text)
        self.space_id = space_id
        self.missing = missing
        self.space_created = space_created


class NetworkError(GoogleChatError):
    """The request got no answer (connection failed, timed out, ...)."""


class APIError(GoogleChatError):
    """The Chat backend answered with an HTTP error."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"{status_code}: {message}")
        self.status_code = status_code
