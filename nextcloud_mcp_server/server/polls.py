"""MCP tools for the Nextcloud Polls app.

Reading needs ``polls.read``; creating and configuring polls, adding options,
voting, commenting, confirming an option and closing or reopening a poll need
``polls.write``. Deleting, archiving and sharing are deliberately not exposed:
the first two cannot be undone from here, and sharing notifies third parties.

Polls is inconsistent about failures: a missing poll is a 400 "Poll not
found" on ``/poll/{id}`` but a 404 carrying the text of the failed SQL query on
the sub-routes (options, comments, votes, close, ...), and a refused action is a
400 whose message starts with "denied" rather than a 403. The translation to
``MCPError`` therefore goes by status *and* message, and lives in one context
manager rather than a try/except per tool. Every response carries a ``url``
that opens the poll.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from httpx import HTTPStatusError, RequestError, Response
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.client.ocs import parse_ocs_envelope
from nextcloud_mcp_server.client.polls import iso_duration
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.links import with_links
from nextcloud_mcp_server.models.polls import (
    DATE_POLL,
    TEXT_POLL,
    AddPollCommentResponse,
    AddPollOptionsResponse,
    ConfirmPollOptionResponse,
    CreatePollResponse,
    GetPollResponse,
    ListPollCommentsResponse,
    ListPollOptionsResponse,
    ListPollsResponse,
    Poll,
    PollAccess,
    PollComment,
    PollOption,
    PollResultsResponse,
    PollSummary,
    PollType,
    UpdatePollResponse,
    VoteAnswer,
    VoteResponse,
    parse_poll_datetime,
    poll_timezone,
    tally_results,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool

logger = logging.getLogger(__name__)

#: Polls' comment field is a TEXT column, but a comment longer than this is
#: almost certainly a mistake by the calling model; reject it with a clear
#: message rather than posting a wall of text into a shared poll.
_COMMENT_MAX_LENGTH = 4000

#: Server messages longer than this are cut before they reach the model.
_SERVER_MESSAGE_MAX_LENGTH = 200


def _server_message(response: Response) -> str | None:
    """The reason Polls gave for a failed request, if it is worth passing on.

    Polls answers a lookup of a missing poll or option on most routes with
    Doctrine's "Did expect one result but found none when executing: query
    ..." -- several hundred characters of SQL that tell the model nothing the
    status does not. Such messages are dropped; any other is cut to
    ``_SERVER_MESSAGE_MAX_LENGTH`` characters.
    """
    try:
        payload = response.json()
    except ValueError:
        return None
    envelope = parse_ocs_envelope(payload)
    message: str | None = None
    # "Unknown error" is parse_ocs_envelope's stand-in for "no message sent".
    if envelope.message != "Unknown error":
        message = envelope.message
    elif isinstance(payload, dict) and isinstance(payload.get("message"), str):
        message = payload["message"]
    if not message:
        return None
    if message.startswith("Did expect one result") or "query" in message:
        return None
    if len(message) > _SERVER_MESSAGE_MAX_LENGTH:
        message = message[: _SERVER_MESSAGE_MAX_LENGTH - 1] + "…"
    return message


def _http_failure(status: int, reason: str | None) -> str:
    """Classify a failed Polls request by status and message."""
    lowered = (reason or "").lower()
    if status == 404 or "not found" in lowered:
        return "not found, or not shared with you"
    if status == 403 or lowered.startswith("denied"):
        return (
            "not allowed — you may lack access or the right to change this "
            "poll, or the poll is closed or locked"
        )
    if 400 <= status < 500:
        return f"request rejected ({status})"
    return f"server error ({status})"


@contextmanager
def _polls_errors(action: str) -> Iterator[None]:
    """Re-raise a Polls failure as the message the model should act on."""
    try:
        yield
    except ValueError as e:
        raise MCPError(code=-1, message=f"Failed {action}: {e}")
    except RequestError as e:
        raise MCPError(code=-1, message=f"Network error {action}: {e}")
    except HTTPStatusError as e:
        reason = _server_message(e.response)
        detail = _http_failure(e.response.status_code, reason)
        if reason:
            detail = f"{detail} ({reason})"
        raise MCPError(code=-1, message=f"Failed {action}: {detail}")


def _validate_timezone(name: str) -> None:
    # Polls stores any string as timezoneName ("Mars/Olympus" included), so
    # this check is the only one a zone name gets.
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(
            f"unknown timezone {name!r}, expected an IANA name like Europe/Berlin"
        ) from e


async def _poll_with_options(
    client: NextcloudClient, poll_id: int, *, votes: bool = True
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """A poll, its options and (optionally) its votes, in as few calls as possible.

    ``/poll/{id}`` already carries ``options`` and ``votes``, so one request
    usually suffices. The dedicated routes are the fallback for a Polls version
    whose ``/poll/{id}`` omits either key.
    """
    details = await client.polls.get_poll_details(poll_id)
    options = details.get("options")
    if options is None:
        options = await client.polls.get_options(poll_id)
    vote_list: list[dict[str, Any]] = []
    if votes:
        listed = details.get("votes")
        vote_list = (
            listed if listed is not None else await client.polls.get_votes(poll_id)
        )
    return details["poll"], options, vote_list


def _is_iana_zone(name: Any) -> bool:
    if not isinstance(name, str) or not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _date_slot(
    start: str, end: str | None, tz: ZoneInfo | timezone
) -> tuple[datetime, int]:
    """Begin and length in seconds of an all-day slot from ``start`` to ``end``.

    Both are dates (YYYY-MM-DD), ``end`` inclusive. The slot runs from midnight
    of ``start`` to midnight after ``end`` in ``tz``, so it is 23 or 25 hours
    long on a day with a DST switch.
    """
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end) if end is not None else first
    except ValueError as e:
        raise ValueError(
            "with all_day, start and end must be dates such as 2026-10-30"
        ) from e
    if last < first:
        raise ValueError("the slot ends before it starts")
    begin = datetime.combine(first, time(0), tzinfo=tz)
    after = datetime.combine(last + timedelta(days=1), time(0), tzinfo=tz)
    # Through timestamps: subtracting two datetimes that share a ZoneInfo
    # ignores the DST switch between them.
    return begin, int(after.timestamp() - begin.timestamp())


def _find_option(
    options: list[dict[str, Any]], option_id: int, poll_id: int
) -> dict[str, Any]:
    for option in options:
        if option.get("id") == option_id and not option.get("deleted"):
            return option
    raise ValueError(f"option {option_id} is not an option of poll {poll_id}")


def _config_changes(
    tz: ZoneInfo | timezone,
    *,
    title: str | None = None,
    description: str | None = None,
    expire: str | None = None,
    remove_expire: bool = False,
    allow_maybe: bool | None = None,
    access: str | None = None,
) -> dict[str, Any]:
    """Polls configuration keys for the arguments that were actually given."""
    if expire is not None and remove_expire:
        raise ValueError("pass either expire or remove_expire, not both")
    changes: dict[str, Any] = {}
    if title is not None:
        if not title.strip():
            raise ValueError("title must not be empty")
        changes["title"] = title
    if description is not None:
        changes["description"] = description
    if expire is not None:
        changes["expire"] = int(parse_poll_datetime(expire, tz).timestamp())
    if remove_expire:
        changes["expire"] = 0  # Polls' "no deadline"
    if allow_maybe is not None:
        changes["allowMaybe"] = allow_maybe
    if access is not None:
        changes["access"] = access
    return changes


def configure_polls_tools(mcp: MCPServer):
    """Configure Polls app MCP tools."""

    # --- Read tools ---

    @mcp.tool(
        title="List Polls",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("polls.read")
    @with_links
    @instrument_tool
    async def nc_polls_list_polls(ctx: Context) -> ListPollsResponse:
        """List every poll the user owns or can access.

        Each entry carries the ID, title, type ('datePoll' for scheduling,
        'textPoll' for text answers), voting deadline, archived/expired flags,
        owner and a link to the poll. Use the ID with the other nc_polls_* tools.
        """
        client = await get_client(ctx)
        with _polls_errors("listing polls"):
            polls = [PollSummary.from_api(p) for p in await client.polls.list_polls()]
        return ListPollsResponse(results=polls, total=len(polls))

    @mcp.tool(
        title="Get Poll",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("polls.read")
    @with_links
    @instrument_tool
    async def nc_polls_get_poll(poll_id: int, ctx: Context) -> GetPollResponse:
        """Get a single poll with its full configuration, status and link."""
        client = await get_client(ctx)
        with _polls_errors(f"getting poll {poll_id}"):
            data = await client.polls.get_poll(poll_id)
        return GetPollResponse(poll=Poll.from_api(data))

    @mcp.tool(
        title="Get Poll Options",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("polls.read")
    @with_links
    @instrument_tool
    async def nc_polls_get_options(
        poll_id: int, ctx: Context
    ) -> ListPollOptionsResponse:
        """Get the options of a poll, in poll order.

        For date polls each option carries its start and end as a datetime in
        the poll's own timezone, along with the current yes/maybe/no counts.
        """
        client = await get_client(ctx)
        with _polls_errors(f"getting options of poll {poll_id}"):
            poll, options, _ = await _poll_with_options(client, poll_id, votes=False)
        poll_type = poll.get("type", "")
        tz_name = (poll.get("configuration") or {}).get("timezoneName")
        tz = poll_timezone(tz_name)
        results = sorted(
            (
                PollOption.from_api(o, poll_type, tz)
                for o in options
                if not o.get("deleted")
            ),
            key=lambda o: (o.order, o.id),
        )
        return ListPollOptionsResponse(
            poll_id=poll_id,
            poll_type=poll_type,
            timezone_name=str(tz),
            results=results,
            total=len(results),
        )

    @mcp.tool(
        title="Get Poll Results",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("polls.read")
    @with_links
    @instrument_tool
    async def nc_polls_get_results(poll_id: int, ctx: Context) -> PollResultsResponse:
        """Get the tally of a poll: per option the yes/maybe/no counts and the
        names of the voters, sorted by approval (most 'yes', then most 'maybe').

        Only votes the user is allowed to see are counted. A poll whose
        results are hidden from the user shows only the user's own votes.
        """
        client = await get_client(ctx)
        with _polls_errors(f"getting results of poll {poll_id}"):
            poll, options, votes = await _poll_with_options(client, poll_id)
        poll_type = poll.get("type", "")
        config = poll.get("configuration") or {}
        tz = poll_timezone(config.get("timezoneName"))
        results, participants = tally_results(options, votes, poll_type, tz)
        return PollResultsResponse(
            poll_id=poll_id,
            title=config.get("title", ""),
            poll_type=poll_type,
            timezone_name=str(tz),
            participants=participants,
            results=results,
            total=len(results),
        )

    @mcp.tool(
        title="Get Poll Comments",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("polls.read")
    @with_links
    @instrument_tool
    async def nc_polls_get_comments(
        poll_id: int, ctx: Context
    ) -> ListPollCommentsResponse:
        """Get the comments on a poll, oldest first.

        Confidential comments are only returned to the poll owner and their
        author. Deleted comments are left out.
        """
        client = await get_client(ctx)
        with _polls_errors(f"getting comments of poll {poll_id}"):
            raw = await client.polls.get_comments(poll_id)
        comments = sorted(
            (PollComment.from_api(c) for c in raw if not c.get("deleted")),
            key=lambda c: (c.created is None, c.created, c.id),
        )
        return ListPollCommentsResponse(
            poll_id=poll_id, results=comments, total=len(comments)
        )

    # --- Write tools ---

    @mcp.tool(
        title="Create Poll",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("polls.write")
    @with_links
    @instrument_tool
    async def nc_polls_create_poll(
        title: str,
        poll_type: PollType,
        ctx: Context,
        description: str | None = None,
        expire: str | None = None,
        allow_maybe: bool | None = None,
        access: PollAccess | None = None,
        timezone_name: str | None = None,
    ) -> CreatePollResponse:
        """Create a poll and return it with its link.

        Args:
            title: Poll title.
            poll_type: 'datePoll' to schedule a date, 'textPoll' for text answers.
            description: Optional description shown above the options.
            expire: Optional voting deadline as ISO 8601, e.g.
                '2026-10-31T18:00'. Without an offset it is read in
                the poll's timezone (see timezone_name).
            allow_maybe: Whether participants may answer 'maybe'.
            access: 'private' (only invited participants, the default) or
                'open' (every user of this Nextcloud can find and join it).
            timezone_name: IANA timezone of the poll, e.g. 'Europe/Berlin'.
                Date options are shown in it. Defaults to the user's own
                timezone in Nextcloud (UTC if that is unknown).

        The poll starts without options: add them with
        nc_polls_add_date_option or nc_polls_add_text_options. A private poll
        is only visible to its owner until it is shared in the Polls app.
        """
        if not title.strip():
            raise MCPError(code=-1, message="Failed creating poll: title is empty")
        client = await get_client(ctx)
        with _polls_errors("creating poll"):
            # Check everything that can be checked before the poll exists, so
            # bad input never leaves a half-configured poll behind. expire is
            # only parsed here; its zone is known after the POST.
            if timezone_name is not None:
                _validate_timezone(timezone_name)
            _config_changes(timezone.utc, expire=expire)
            created = await client.polls.create_poll(title, poll_type)
            # Polls 9.3 passes votingVariant into PollService::add's
            # timezoneName parameter, so every poll created over the API has
            # timezoneName "simple". The zone is therefore always set in the
            # follow-up PUT: the given one, else the owner's own zone from the
            # POST answer, else UTC.
            if timezone_name is None:
                owner_zone = (created.get("owner") or {}).get("timeZone")
                if _is_iana_zone(owner_zone):
                    timezone_name = owner_zone
                else:
                    logger.debug(
                        "Owner timezone %r of new poll %s unusable, using UTC",
                        owner_zone,
                        created["id"],
                    )
                    timezone_name = "UTC"
            changes = _config_changes(
                poll_timezone(timezone_name),
                description=description,
                expire=expire,
                allow_maybe=allow_maybe,
                access=access,
            )
            changes["timezoneName"] = timezone_name
            # The POST answer carries a stale configuration; the PUT answer is
            # the poll as stored.
            poll = await client.polls.update_poll(created["id"], changes)
        return CreatePollResponse(poll=Poll.from_api(poll))

    @mcp.tool(
        title="Update Poll",
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("polls.write")
    @with_links
    @instrument_tool
    async def nc_polls_update_poll(
        poll_id: int,
        ctx: Context,
        title: str | None = None,
        description: str | None = None,
        expire: str | None = None,
        remove_expire: bool = False,
        allow_maybe: bool | None = None,
        access: PollAccess | None = None,
    ) -> UpdatePollResponse:
        """Change the settings of a poll. Only the arguments given are changed.

        Args:
            poll_id: Poll to change.
            title: New title.
            description: New description.
            expire: New voting deadline as ISO 8601. Without an offset it is
                read in the poll's timezone.
            remove_expire: True to remove the voting deadline.
            allow_maybe: Whether participants may answer 'maybe'.
            access: 'private' or 'open'.
        """
        client = await get_client(ctx)
        with _polls_errors(f"updating poll {poll_id}"):
            tz: ZoneInfo | timezone = timezone.utc
            if expire is not None:
                current = await client.polls.get_poll(poll_id)
                tz = poll_timezone(
                    (current.get("configuration") or {}).get("timezoneName")
                )
            changes = _config_changes(
                tz,
                title=title,
                description=description,
                expire=expire,
                remove_expire=remove_expire,
                allow_maybe=allow_maybe,
                access=access,
            )
            if not changes:
                raise ValueError("nothing to change, pass at least one setting")
            poll = await client.polls.update_poll(poll_id, changes)
        return UpdatePollResponse(poll=Poll.from_api(poll))

    @mcp.tool(
        title="Add Date Option to Poll",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("polls.write")
    @with_links
    @instrument_tool
    async def nc_polls_add_date_option(
        poll_id: int,
        start: str,
        ctx: Context,
        end: str | None = None,
        duration_minutes: int | None = None,
        all_day: bool = False,
    ) -> AddPollOptionsResponse:
        """Add a date/time slot to a date poll.

        Args:
            poll_id: A poll of type 'datePoll'.
            start: Start as ISO 8601, e.g. '2026-10-20T18:00'. Without an
                offset it is read in the poll's timezone (UTC if it has none).
                With all_day, a date such as '2026-10-30'.
            end: End as ISO 8601. Give end or duration_minutes, not both.
                With neither, the slot is a point in time. With all_day, the
                last day (inclusive) as a date, by default start.
            duration_minutes: Length of the slot in minutes. Not with all_day.
            all_day: True for a slot of whole days, from midnight of start to
                midnight after end in the poll's timezone. Whether the Polls
                web interface shows such a slot as all-day is not verified.

        The response shows the slot as stored, with an explicit offset, so it
        can be checked against what was meant. If the poll already has a slot
        with the same start and length, nothing is added and note says so.
        """
        if end is not None and duration_minutes is not None:
            raise MCPError(
                code=-1,
                message="Failed adding date option: pass end or duration_minutes, not both",
            )
        if all_day and duration_minutes is not None:
            raise MCPError(
                code=-1,
                message="Failed adding date option: duration_minutes does not apply to all_day",
            )
        client = await get_client(ctx)
        with _polls_errors(f"adding a date option to poll {poll_id}"):
            poll, before, _ = await _poll_with_options(client, poll_id, votes=False)
            if poll.get("type") != DATE_POLL:
                raise ValueError(
                    "poll is not a date poll, use nc_polls_add_text_options"
                )
            tz = poll_timezone((poll.get("configuration") or {}).get("timezoneName"))
            if all_day:
                begin, duration = _date_slot(start, end, tz)
                # Hours, not days: Polls adds a D part in the start's fixed
                # offset, which is an hour off across a DST switch.
                length = iso_duration(duration, use_days=False)
            else:
                begin = parse_poll_datetime(start, tz).astimezone(tz)
                if end is not None:
                    # Through timestamps: subtracting two datetimes that share
                    # a ZoneInfo ignores a DST switch between them.
                    duration = int(
                        parse_poll_datetime(end, tz).timestamp() - begin.timestamp()
                    )
                else:
                    duration = (duration_minutes or 0) * 60
                if duration < 0:
                    raise ValueError("the slot ends before it starts")
                length = iso_duration(duration)
            known = {o.get("id") for o in before if not o.get("deleted")}
            option = await client.polls.add_option(
                poll_id, begin.isoformat(timespec="seconds"), length
            )
        # Polls answers 201 with the existing option when the slot is already
        # in the poll; it is reported as such rather than as added.
        if option.get("id") in known:
            return AddPollOptionsResponse(
                poll_id=poll_id,
                timezone_name=str(tz),
                added=[],
                note=(
                    f"nothing added: the poll already has this slot as option "
                    f"{option.get('id')}"
                ),
            )
        return AddPollOptionsResponse(
            poll_id=poll_id,
            timezone_name=str(tz),
            added=[PollOption.from_api(option, DATE_POLL, tz)],
        )

    @mcp.tool(
        title="Add Text Options to Poll",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("polls.write")
    @with_links
    @instrument_tool
    async def nc_polls_add_text_options(
        poll_id: int, texts: list[str], ctx: Context
    ) -> AddPollOptionsResponse:
        """Add one or more text answers to a text poll in a single call.

        Args:
            poll_id: A poll of type 'textPoll'.
            texts: The answers to add, one per entry. An entry must not be
                empty or contain a line break.
        """
        cleaned = [t.strip() for t in texts]
        if not cleaned or any(not t or "\n" in t or "\r" in t for t in cleaned):
            raise MCPError(
                code=-1,
                message=(
                    "Failed adding text options: pass at least one text, none "
                    "empty and none containing a line break"
                ),
            )
        client = await get_client(ctx)
        with _polls_errors(f"adding text options to poll {poll_id}"):
            poll, before, _ = await _poll_with_options(client, poll_id, votes=False)
            if poll.get("type") != TEXT_POLL:
                raise ValueError(
                    "poll is not a text poll, use nc_polls_add_date_option"
                )
            known = {o.get("id") for o in before}
            after = await client.polls.add_text_options(poll_id, cleaned)
        tz = poll_timezone((poll.get("configuration") or {}).get("timezoneName"))
        added = [
            PollOption.from_api(o, TEXT_POLL, tz)
            for o in after
            if o.get("id") not in known and not o.get("deleted")
        ]
        return AddPollOptionsResponse(
            poll_id=poll_id, timezone_name=str(tz), added=added
        )

    @mcp.tool(
        title="Vote in Poll",
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("polls.write")
    @with_links
    @instrument_tool
    async def nc_polls_vote(
        poll_id: int, option_id: int, answer: VoteAnswer, ctx: Context
    ) -> VoteResponse:
        """Record the user's answer on one option of a poll.

        Args:
            poll_id: The poll the option belongs to.
            option_id: Option to answer, from nc_polls_get_options.
            answer: 'yes', 'maybe' (only if the poll allows it) or 'no'.

        Voting again on the same option replaces the earlier answer.
        """
        client = await get_client(ctx)
        with _polls_errors(f"voting on option {option_id} of poll {poll_id}"):
            poll, options, _ = await _poll_with_options(client, poll_id, votes=False)
            _find_option(options, option_id, poll_id)
            config = poll.get("configuration") or {}
            # Polls 9.3 records a 'maybe' even when the poll does not allow
            # it, so this check is the only one.
            if answer == "maybe" and not config.get("allowMaybe", False):
                raise ValueError("this poll does not allow 'maybe' answers")
            vote = await client.polls.vote(option_id, answer)
        recorded = (vote or {}).get("answer") or answer
        return VoteResponse(poll_id=poll_id, option_id=option_id, answer=recorded)

    @mcp.tool(
        title="Comment on Poll",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )
    @require_scopes("polls.write")
    @with_links
    @instrument_tool
    async def nc_polls_add_comment(
        poll_id: int, comment: str, ctx: Context, confidential: bool = False
    ) -> AddPollCommentResponse:
        """Add a comment to a poll, visible to its participants.

        Args:
            poll_id: Poll to comment on.
            comment: Comment text.
            confidential: True to make the comment visible only to the poll
                owner and the author.
        """
        if not comment.strip():
            raise MCPError(code=-1, message="Failed adding comment: comment is empty")
        if len(comment) > _COMMENT_MAX_LENGTH:
            raise MCPError(
                code=-1,
                message=(
                    f"Failed adding comment: {len(comment)} characters "
                    f"(max {_COMMENT_MAX_LENGTH})"
                ),
            )
        client = await get_client(ctx)
        with _polls_errors(f"commenting on poll {poll_id}"):
            data = await client.polls.add_comment(poll_id, comment, confidential)
        return AddPollCommentResponse(
            poll_id=poll_id, comment=PollComment.from_api(data)
        )

    @mcp.tool(
        title="Confirm Poll Option",
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("polls.write")
    @with_links
    @instrument_tool
    async def nc_polls_confirm_option(
        poll_id: int, option_id: int, ctx: Context, confirmed: bool = True
    ) -> ConfirmPollOptionResponse:
        """Mark an option as the confirmed outcome of a poll, or unmark it.

        Typically used by the poll owner after closing a date poll to fix the
        chosen date. Several options may be confirmed.

        Args:
            poll_id: The poll the option belongs to.
            option_id: Option to confirm, from nc_polls_get_options.
            confirmed: True to confirm, False to withdraw the confirmation.
        """
        client = await get_client(ctx)
        with _polls_errors(f"confirming option {option_id} of poll {poll_id}"):
            poll, options, _ = await _poll_with_options(client, poll_id, votes=False)
            option = _find_option(options, option_id, poll_id)
            # Polls only offers a toggle; read the state first so calling this
            # twice with the same argument leaves the option as asked.
            changed = bool(option.get("confirmed")) != confirmed
            if changed:
                option = await client.polls.toggle_option_confirmation(option_id)
        poll_type = poll.get("type", "")
        tz = poll_timezone((poll.get("configuration") or {}).get("timezoneName"))
        return ConfirmPollOptionResponse(
            poll_id=poll_id,
            option=PollOption.from_api(option, poll_type, tz),
            changed=changed,
        )

    @mcp.tool(
        title="Close Poll",
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("polls.write")
    @with_links
    @instrument_tool
    async def nc_polls_close_poll(poll_id: int, ctx: Context) -> UpdatePollResponse:
        """Close a poll so no further votes can be cast. Reversible with
        nc_polls_reopen_poll.

        Polls closes a poll by setting its deadline to the moment of closing.
        Commenting stays possible, and confirmed options stay confirmed.
        """
        client = await get_client(ctx)
        with _polls_errors(f"closing poll {poll_id}"):
            poll = await client.polls.close_poll(poll_id)
        return UpdatePollResponse(poll=Poll.from_api(poll))

    @mcp.tool(
        title="Reopen Poll",
        annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True),
    )
    @require_scopes("polls.write")
    @with_links
    @instrument_tool
    async def nc_polls_reopen_poll(poll_id: int, ctx: Context) -> UpdatePollResponse:
        """Reopen a closed poll for voting.

        Reopening removes the deadline entirely: the poll is open without a
        deadline afterwards and the earlier deadline is lost. Set a new one
        with nc_polls_update_poll if needed.
        """
        client = await get_client(ctx)
        with _polls_errors(f"reopening poll {poll_id}"):
            poll = await client.polls.reopen_poll(poll_id)
        return UpdatePollResponse(poll=Poll.from_api(poll))
