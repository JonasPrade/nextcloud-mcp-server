"""Pydantic models for the Nextcloud Polls app.

The app's wire format (camelCase, Unix timestamps, ``0`` for "not set") is
translated here into snake_case fields and timezone-aware datetimes, so the
model reading a tool result does not have to know Polls' conventions.
"""

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from .base import BaseResponse

#: Poll types the app knows. Kept as ``str`` on the models so a future type
#: does not fail validation, but these are the two the tools special-case.
DATE_POLL = "datePoll"
TEXT_POLL = "textPoll"


def poll_timezone(name: str | None) -> ZoneInfo | timezone:
    """The poll's timezone, falling back to UTC for a missing or unknown name."""
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone.utc


def from_unix(value: Any, tz: ZoneInfo | timezone = timezone.utc) -> datetime | None:
    """A Polls Unix timestamp as an aware datetime; ``0``/missing means unset."""
    if not value:
        return None
    return datetime.fromtimestamp(int(value), tz=tz)


# Domain models


class PollOwner(BaseModel):
    """The owner of a poll."""

    user_id: str = Field(description="Nextcloud user ID of the owner")
    display_name: str = Field(description="Display name of the owner")

    @classmethod
    def from_api(cls, data: dict[str, Any] | None) -> "PollOwner":
        data = data or {}
        user_id = data.get("userId") or data.get("id") or ""
        return cls(user_id=user_id, display_name=data.get("displayName") or user_id)


class PollSummary(BaseModel):
    """One entry of the poll list."""

    id: int = Field(description="Poll ID")
    title: str = Field(description="Poll title")
    type: str = Field(description="Poll type: 'datePoll' or 'textPoll'")
    voting_variant: str | None = Field(
        default=None, description="Voting variant, e.g. 'simple'"
    )
    access: str | None = Field(
        default=None, description="Access level: 'private' or 'open'"
    )
    expire: datetime | None = Field(
        default=None,
        description="Voting deadline in the poll's timezone; null if none is set",
    )
    is_archived: bool = Field(default=False, description="Poll is archived")
    is_expired: bool = Field(default=False, description="Voting deadline has passed")
    owner: PollOwner = Field(description="Owner of the poll")

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "PollSummary":
        config = data.get("configuration") or {}
        status = data.get("status") or {}
        tz = poll_timezone(config.get("timezoneName"))
        return cls(
            id=data["id"],
            title=config.get("title", ""),
            type=data.get("type", ""),
            voting_variant=data.get("votingVariant"),
            access=config.get("access"),
            expire=from_unix(config.get("expire"), tz),
            is_archived=bool(status.get("isArchived", False)),
            is_expired=bool(status.get("isExpired", False)),
            owner=PollOwner.from_api(data.get("owner")),
        )


class PollConfiguration(BaseModel):
    """The settings of a poll."""

    title: str = Field(description="Poll title")
    description: str = Field(default="", description="Poll description")
    access: str | None = Field(
        default=None, description="Access level: 'private' or 'open'"
    )
    allow_maybe: bool = Field(default=False, description="'Maybe' answers allowed")
    allow_comment: bool = Field(default=False, description="Comments allowed")
    allow_proposals: str | None = Field(
        default=None, description="Whether participants may propose options"
    )
    anonymous: bool = Field(default=False, description="Voters are hidden")
    use_no: bool = Field(default=True, description="'No' is a recorded answer")
    show_results: str | None = Field(
        default=None,
        description="When results are visible: 'always', 'closed' or 'never'",
    )
    expire: datetime | None = Field(
        default=None,
        description="Voting deadline in the poll's timezone; null if none is set",
    )
    timezone_name: str | None = Field(
        default=None, description="IANA timezone the poll was created in"
    )
    max_votes_per_option: int = Field(
        default=0, description="Maximum 'yes' votes per option; 0 = unlimited"
    )
    max_votes_per_user: int = Field(
        default=0, description="Maximum 'yes' votes per user; 0 = unlimited"
    )

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "PollConfiguration":
        tz = poll_timezone(data.get("timezoneName"))
        return cls(
            title=data.get("title", ""),
            description=data.get("description") or "",
            access=data.get("access"),
            allow_maybe=bool(data.get("allowMaybe", False)),
            allow_comment=bool(data.get("allowComment", False)),
            allow_proposals=data.get("allowProposals"),
            anonymous=bool(data.get("anonymous", False)),
            use_no=bool(data.get("useNo", True)),
            show_results=data.get("showResults"),
            expire=from_unix(data.get("expire"), tz),
            timezone_name=data.get("timezoneName"),
            max_votes_per_option=int(data.get("maxVotesPerOption") or 0),
            max_votes_per_user=int(data.get("maxVotesPerUser") or 0),
        )


class PollStatus(BaseModel):
    """Lifecycle state of a poll."""

    created: datetime | None = Field(default=None, description="Creation time (UTC)")
    last_interaction: datetime | None = Field(
        default=None, description="Time of the last vote, comment or change (UTC)"
    )
    is_archived: bool = Field(default=False, description="Poll is archived")
    is_expired: bool = Field(default=False, description="Voting deadline has passed")
    is_anonymous: bool = Field(default=False, description="Voters are hidden")
    count_participants: int | None = Field(
        default=None, description="Number of participants who voted"
    )

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "PollStatus":
        count = data.get("countParticipants")
        return cls(
            created=from_unix(data.get("created")),
            last_interaction=from_unix(data.get("lastInteraction")),
            is_archived=bool(data.get("isArchived", False)),
            is_expired=bool(data.get("isExpired", False)),
            is_anonymous=bool(data.get("isAnonymous", False)),
            count_participants=int(count) if count is not None else None,
        )


class Poll(BaseModel):
    """A poll with its configuration and status."""

    id: int = Field(description="Poll ID")
    type: str = Field(description="Poll type: 'datePoll' or 'textPoll'")
    voting_variant: str | None = Field(
        default=None, description="Voting variant, e.g. 'simple'"
    )
    configuration: PollConfiguration = Field(description="Poll settings")
    owner: PollOwner = Field(description="Owner of the poll")
    status: PollStatus = Field(description="Lifecycle state of the poll")

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Poll":
        return cls(
            id=data["id"],
            type=data.get("type", ""),
            voting_variant=data.get("votingVariant"),
            configuration=PollConfiguration.from_api(data.get("configuration") or {}),
            owner=PollOwner.from_api(data.get("owner")),
            status=PollStatus.from_api(data.get("status") or {}),
        )


class PollOption(BaseModel):
    """One option of a poll: a date/time slot or a text answer."""

    id: int = Field(description="Option ID")
    text: str = Field(description="Option text as stored by Polls")
    order: int = Field(default=0, description="Position of the option in the poll")
    confirmed: bool = Field(
        default=False, description="The owner has confirmed this option"
    )
    start: datetime | None = Field(
        default=None,
        description="Date polls only: start of the slot in the poll's timezone",
    )
    end: datetime | None = Field(
        default=None,
        description="Date polls only: end of the slot in the poll's timezone",
    )
    yes: int = Field(default=0, description="Number of 'yes' votes")
    maybe: int = Field(default=0, description="Number of 'maybe' votes")
    no: int = Field(default=0, description="Number of 'no' votes")

    @classmethod
    def from_api(
        cls, data: dict[str, Any], poll_type: str, tz: ZoneInfo | timezone
    ) -> "PollOption":
        start, end = option_span(data, poll_type, tz)
        votes = data.get("votes") or {}
        return cls(
            id=data["id"],
            text=str(data.get("text", "")),
            order=int(data.get("order") or 0),
            confirmed=bool(data.get("confirmed")),
            start=start,
            end=end,
            yes=int(votes.get("yes") or 0),
            maybe=int(votes.get("maybe") or 0),
            no=int(votes.get("no") or 0),
        )


def option_span(
    data: dict[str, Any], poll_type: str, tz: ZoneInfo | timezone
) -> tuple[datetime | None, datetime | None]:
    """Start and end of a date poll option; ``(None, None)`` for text polls.

    Polls stores a slot as a Unix ``timestamp`` plus a ``duration`` in seconds.
    A zero duration is a point in time, so end equals start.
    """
    if poll_type != DATE_POLL:
        return None, None
    start = from_unix(data.get("timestamp"), tz)
    if start is None:
        return None, None
    end = from_unix(int(data["timestamp"]) + int(data.get("duration") or 0), tz)
    return start, end


class PollOptionResult(BaseModel):
    """Tally of one option, with the names of the voters per answer."""

    option_id: int = Field(description="Option ID")
    text: str = Field(description="Option text as stored by Polls")
    start: datetime | None = Field(
        default=None,
        description="Date polls only: start of the slot in the poll's timezone",
    )
    end: datetime | None = Field(
        default=None,
        description="Date polls only: end of the slot in the poll's timezone",
    )
    confirmed: bool = Field(
        default=False, description="The owner has confirmed this option"
    )
    yes: int = Field(description="Number of 'yes' votes")
    maybe: int = Field(description="Number of 'maybe' votes")
    no: int = Field(description="Number of 'no' votes")
    yes_voters: list[str] = Field(description="Names of those who voted 'yes'")
    maybe_voters: list[str] = Field(description="Names of those who voted 'maybe'")
    no_voters: list[str] = Field(description="Names of those who voted 'no'")


def _voter_name(vote: dict[str, Any]) -> str:
    user = vote.get("user") or {}
    return user.get("displayName") or user.get("userId") or user.get("id") or "?"


def tally_results(
    options: list[dict[str, Any]],
    votes: list[dict[str, Any]],
    poll_type: str,
    tz: ZoneInfo | timezone,
) -> tuple[list[PollOptionResult], list[str]]:
    """Count the answers per option and sort the options by approval.

    Counts come from the vote list rather than the counters on each option, so
    every number matches the names next to it. Deleted options and votes are
    skipped. Options sort by most 'yes', then most 'maybe', then poll order.

    Returns the sorted results and the sorted names of all participants.
    """
    voters: dict[int, dict[str, list[str]]] = {}
    participants: set[str] = set()
    for vote in votes:
        if vote.get("deleted"):
            continue
        answer = vote.get("answer")
        if answer not in ("yes", "maybe", "no"):
            continue
        name = _voter_name(vote)
        participants.add(name)
        option_voters = voters.setdefault(
            vote.get("optionId") or 0, {"yes": [], "maybe": [], "no": []}
        )
        option_voters[answer].append(name)

    results: list[tuple[int, PollOptionResult]] = []
    for option in options:
        if option.get("deleted"):
            continue
        start, end = option_span(option, poll_type, tz)
        names = voters.get(option["id"], {"yes": [], "maybe": [], "no": []})
        results.append(
            (
                int(option.get("order") or 0),
                PollOptionResult(
                    option_id=option["id"],
                    text=str(option.get("text", "")),
                    start=start,
                    end=end,
                    confirmed=bool(option.get("confirmed")),
                    yes=len(names["yes"]),
                    maybe=len(names["maybe"]),
                    no=len(names["no"]),
                    yes_voters=sorted(names["yes"]),
                    maybe_voters=sorted(names["maybe"]),
                    no_voters=sorted(names["no"]),
                ),
            )
        )
    results.sort(key=lambda r: (-r[1].yes, -r[1].maybe, r[0], r[1].option_id))
    return [result for _, result in results], sorted(participants)


# Response models


class ListPollsResponse(BaseResponse):
    """Response model for listing polls."""

    results: list[PollSummary] = Field(description="Polls the user owns or can access")
    total: int = Field(description="Number of polls returned")


class GetPollResponse(BaseResponse):
    """Response model for fetching a single poll."""

    poll: Poll = Field(description="The poll")


class ListPollOptionsResponse(BaseResponse):
    """Response model for listing a poll's options."""

    poll_id: int = Field(description="Poll ID")
    poll_type: str = Field(description="Poll type: 'datePoll' or 'textPoll'")
    timezone_name: str = Field(
        description="Timezone the option start/end times are expressed in"
    )
    results: list[PollOption] = Field(description="Options in poll order")
    total: int = Field(description="Number of options returned")


class PollResultsResponse(BaseResponse):
    """Response model for the tally of a poll."""

    poll_id: int = Field(description="Poll ID")
    title: str = Field(description="Poll title")
    poll_type: str = Field(description="Poll type: 'datePoll' or 'textPoll'")
    timezone_name: str = Field(
        description="Timezone the option start/end times are expressed in"
    )
    participants: list[str] = Field(
        description="Names of everyone whose votes are visible to the user"
    )
    results: list[PollOptionResult] = Field(
        description="Options sorted by approval: most 'yes', then most 'maybe'"
    )
    total: int = Field(description="Number of options returned")
