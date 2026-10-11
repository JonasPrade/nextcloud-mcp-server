"""Client for the Nextcloud Polls app (``polls``).

Covers the read side of the app's OCS API: the poll list, a single poll, and a
poll's options and votes. Writing (creating polls, voting) is deliberately not
wrapped yet.

Field names follow the app's ``openapi.json``
(https://github.com/nextcloud/polls/blob/main/openapi.json).
"""

import logging
from typing import Any

from .base import BaseNextcloudClient
from .ocs import OCS_REQUEST_HEADERS

logger = logging.getLogger(__name__)


def _validate_poll_id(poll_id: int) -> None:
    """Reject ids that cannot address a poll before they reach the URL.

    ``bool`` is excluded explicitly: it is an ``int`` subclass, so ``True``
    would otherwise sail through as poll 1.
    """
    if isinstance(poll_id, bool) or not isinstance(poll_id, int):
        raise ValueError(f"Poll ID must be an integer: {poll_id!r}")
    if poll_id <= 0:
        raise ValueError(f"Poll ID must be positive: {poll_id}")


class PollsClient(BaseNextcloudClient):
    """Client for Nextcloud Polls app operations."""

    app_name = "polls"

    API_BASE = "/ocs/v2.php/apps/polls/api/v1.0"

    # Own copy -- see the note in GroupsClient.
    _OCS_HEADERS: dict[str, str] = dict(OCS_REQUEST_HEADERS)

    async def _ocs(self, path: str) -> Any:
        """GET an OCS route and hand back ``ocs.data``.

        ``/ocs/v2.php`` mirrors the OCS code onto the HTTP status, which
        ``_make_request`` already raises on, so unwrapping is all that is left.
        """
        response = await self._make_request(
            "GET",
            f"{self.API_BASE}{path}",
            headers=self._OCS_HEADERS,
            params={"format": "json"},
        )
        return response.json()["ocs"]["data"]

    async def list_polls(self) -> list[dict[str, Any]]:
        """Get every poll the user owns or can access."""
        data = await self._ocs("/polls")
        return data.get("polls") or []

    async def get_poll_details(self, poll_id: int) -> dict[str, Any]:
        """Get the full ``/poll/{id}`` payload.

        Besides ``poll`` the route answers with the poll's ``options`` and
        ``votes`` (plus comments and shares), which saves the tools two round
        trips.

        Raises:
            HTTPStatusError: 404 if the poll does not exist or is not
                accessible to the user.
        """
        _validate_poll_id(poll_id)
        return await self._ocs(f"/poll/{poll_id}")

    async def get_poll(self, poll_id: int) -> dict[str, Any]:
        """Get a single poll with its configuration and status."""
        return (await self.get_poll_details(poll_id))["poll"]

    async def get_options(self, poll_id: int) -> list[dict[str, Any]]:
        """Get the options (dates or text answers) of a poll."""
        _validate_poll_id(poll_id)
        data = await self._ocs(f"/poll/{poll_id}/options")
        return data.get("options") or []

    async def get_votes(self, poll_id: int) -> list[dict[str, Any]]:
        """Get the votes of a poll the user is allowed to see."""
        _validate_poll_id(poll_id)
        data = await self._ocs(f"/poll/{poll_id}/votes")
        return data.get("votes") or []
