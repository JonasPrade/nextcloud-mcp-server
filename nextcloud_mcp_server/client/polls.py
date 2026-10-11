"""Client for the Nextcloud Polls app (``polls``).

Covers the app's OCS API for reading polls, options, votes and comments, and
for the writes a participant or organiser needs: creating and configuring a
poll, adding options, voting, commenting, confirming an option, and closing or
reopening a poll. Deleting, archiving and sharing (which notifies third
parties) are deliberately not wrapped.

Field names follow the app's ``openapi.json``
(https://github.com/nextcloud/polls/blob/main/openapi.json).
"""

import logging
from typing import Any

from .base import BaseNextcloudClient
from .ocs import OCS_REQUEST_HEADERS

logger = logging.getLogger(__name__)

#: The answers Polls accepts on its vote route.
VOTE_ANSWERS = frozenset({"yes", "no", "maybe"})


def _validate_id(value: int, what: str) -> None:
    """Reject ids that cannot address a poll or option before they reach the URL.

    ``bool`` is excluded explicitly: it is an ``int`` subclass, so ``True``
    would otherwise sail through as id 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} ID must be an integer: {value!r}")
    if value <= 0:
        raise ValueError(f"{what} ID must be positive: {value}")


def _validate_poll_id(poll_id: int) -> None:
    _validate_id(poll_id, "Poll")


def iso_duration(seconds: int, *, use_days: bool = True) -> str | None:
    """A length in seconds as an ISO 8601 duration, ``None`` for zero.

    Polls parses ``isoDuration`` with PHP's ``DateInterval`` and answers a
    malformed one with HTTP 500, so only the canonical
    ``P[nD][T[nH][nM][nS]]`` form is produced: 5400 → ``PT1H30M``, 7200 →
    ``PT2H``, 93600 → ``P1DT2H``. Zero yields ``None`` because Polls stores a
    slot without duration as a point in time.

    ``use_days=False`` keeps whole days as hours (86400 → ``PT24H``). Polls adds
    a ``D`` part in the fixed offset of the start, not in the poll's zone, so a
    span across a DST switch must be sent in hours to come out right.

    Raises:
        ValueError: if ``seconds`` is negative.
    """
    if seconds < 0:
        raise ValueError(f"duration must not be negative: {seconds}")
    if seconds == 0:
        return None
    days, rest = divmod(seconds, 86400) if use_days else (0, seconds)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    time_part = "".join(
        f"{value}{unit}"
        for value, unit in ((hours, "H"), (minutes, "M"), (secs, "S"))
        if value
    )
    return "P" + (f"{days}D" if days else "") + (f"T{time_part}" if time_part else "")


class PollsClient(BaseNextcloudClient):
    """Client for Nextcloud Polls app operations."""

    app_name = "polls"

    API_BASE = "/ocs/v2.php/apps/polls/api/v1.0"

    # Own copy -- see the note in GroupsClient.
    _OCS_HEADERS: dict[str, str] = dict(OCS_REQUEST_HEADERS)

    async def _ocs(
        self, path: str, method: str = "GET", json: dict[str, Any] | None = None
    ) -> Any:
        """Call an OCS route and hand back ``ocs.data``.

        ``/ocs/v2.php`` mirrors the OCS code onto the HTTP status, which
        ``_make_request`` already raises on, so unwrapping is all that is left.
        ``json`` is only sent when given, so bodyless calls carry no
        ``Content-Type`` header.
        """
        kwargs: dict[str, Any] = {
            "headers": self._OCS_HEADERS,
            "params": {"format": "json"},
        }
        if json is not None:
            kwargs["json"] = json
        response = await self._make_request(method, f"{self.API_BASE}{path}", **kwargs)
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
            HTTPStatusError: 400 ("Poll not found") if the poll does not
                exist or is not accessible to the user.
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

    async def get_comments(self, poll_id: int) -> list[dict[str, Any]]:
        """Get the comments of a poll the user is allowed to see."""
        _validate_poll_id(poll_id)
        data = await self._ocs(f"/poll/{poll_id}/comments")
        return data.get("comments") or []

    # --- Writes ---

    async def create_poll(
        self, title: str, poll_type: str, voting_variant: str = "simple"
    ) -> dict[str, Any]:
        """Create a poll with default settings and return it."""
        data = await self._ocs(
            "/poll",
            "POST",
            json={"title": title, "type": poll_type, "votingVariant": voting_variant},
        )
        return data["poll"]

    async def update_poll(
        self, poll_id: int, configuration: dict[str, Any]
    ) -> dict[str, Any]:
        """Change the given configuration keys of a poll and return it.

        Keys are Polls' own camelCase configuration names (``title``,
        ``description``, ``expire``, ``allowMaybe``, ``access``, ...); keys not
        given keep their value.
        """
        _validate_poll_id(poll_id)
        data = await self._ocs(
            f"/poll/{poll_id}", "PUT", json={"pollConfiguration": configuration}
        )
        return data["poll"]

    async def add_option(
        self, poll_id: int, iso_timestamp: str, iso_duration: str | None = None
    ) -> dict[str, Any]:
        """Add a date option and return it.

        ``iso_timestamp`` is the start as ISO 8601 with offset, ``iso_duration``
        its length as an ISO 8601 duration (``None`` for a point in time).
        Polls keeps the offset of ``isoTimestamp`` in the option text, as its
        web UI does. A Unix ``timestamp`` is never sent: Polls prefers it over
        ``isoTimestamp`` and then writes the text in UTC.

        An option with the same start and duration as an existing one is not
        created again; Polls answers 201 with the existing option.
        """
        _validate_poll_id(poll_id)
        option: dict[str, Any] = {"isoTimestamp": iso_timestamp}
        if iso_duration is not None:
            option["isoDuration"] = iso_duration
        data = await self._ocs(
            f"/poll/{poll_id}/option", "POST", json={"option": option}
        )
        return data["option"]

    async def add_text_options(
        self, poll_id: int, texts: list[str]
    ) -> list[dict[str, Any]]:
        """Add text options in one call and return the poll's options.

        The route takes the options newline-separated in a single string.
        """
        _validate_poll_id(poll_id)
        data = await self._ocs(
            f"/poll/{poll_id}/options", "POST", json={"text": "\n".join(texts)}
        )
        return data.get("options") or []

    async def vote(self, option_id: int, answer: str) -> dict[str, Any] | None:
        """Set the user's answer (``yes``, ``no`` or ``maybe``) on an option."""
        _validate_id(option_id, "Option")
        # The answer goes into the URL path, so it is checked against the
        # three values Polls accepts before the request is built.
        if answer not in VOTE_ANSWERS:
            raise ValueError(
                f"Answer must be one of {sorted(VOTE_ANSWERS)}: {answer!r}"
            )
        data = await self._ocs(f"/option/{option_id}/vote/{answer}", "PUT")
        return data.get("vote")

    async def toggle_option_confirmation(self, option_id: int) -> dict[str, Any]:
        """Flip an option's confirmed state and return the option.

        The route is a toggle, not a setter: calling it twice undoes it.
        """
        _validate_id(option_id, "Option")
        data = await self._ocs(f"/option/{option_id}/confirm", "PUT")
        return data["option"]

    async def add_comment(
        self, poll_id: int, comment: str, confidential: bool = False
    ) -> dict[str, Any]:
        """Add a comment to a poll and return it."""
        _validate_poll_id(poll_id)
        data = await self._ocs(
            f"/poll/{poll_id}/comment",
            "POST",
            json={"comment": comment, "confidential": confidential},
        )
        return data["comment"]

    async def close_poll(self, poll_id: int) -> dict[str, Any]:
        """Close a poll for voting and return it.

        Polls closes a poll by setting ``expire`` to the moment of closing.
        """
        _validate_poll_id(poll_id)
        data = await self._ocs(f"/poll/{poll_id}/close", "PUT")
        return data["poll"]

    async def reopen_poll(self, poll_id: int) -> dict[str, Any]:
        """Reopen a closed poll and return it.

        Polls sets ``expire`` to 0 on reopening: the poll has no deadline
        afterwards, the earlier one is gone.
        """
        _validate_poll_id(poll_id)
        data = await self._ocs(f"/poll/{poll_id}/reopen", "PUT")
        return data["poll"]
