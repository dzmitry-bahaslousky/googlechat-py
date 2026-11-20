"""Errors raised by the Google Chat client."""


class GoogleChatError(Exception):
    """Base class for every error this library raises."""


class NotSignedInError(GoogleChatError):
    """No usable saved session: cookies are missing or incomplete."""


class SessionExpiredError(GoogleChatError):
    """Google rejected the saved session; sign in again."""


class SpaceNotFoundError(GoogleChatError):
    """A space id that is not in your chat list, so whether it is a DM is unknown."""


class NetworkError(GoogleChatError):
    """The request got no answer (connection failed, timed out, ...)."""


class APIError(GoogleChatError):
    """The Chat backend answered with an HTTP error."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"{status_code}: {message}")
        self.status_code = status_code
