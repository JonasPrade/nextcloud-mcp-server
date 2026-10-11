"""MCP tools for the Nextcloud Polls app (read-only).

Polls answers a missing poll with 404 and a poll the user may not see with
403, so the translation to ``MCPError`` lives in one context manager rather
than a try/except per tool.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from httpx import HTTPStatusError, RequestError
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import ToolAnnotations

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.polls import (
    GetPollResponse,
    ListPollOptionsResponse,
    ListPollsResponse,
    Poll,
    PollOption,
    PollResultsResponse,
    PollSummary,
    poll_timezone,
    tally_results,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool

logger = logging.getLogger(__name__)


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
        status = e.response.status_code
        if status == 404:
            detail = "poll not found, or not shared with you"
        elif status == 403:
            detail = "you do not have access to this poll"
        else:
            detail = f"server error ({status})"
        raise MCPError(code=-1, message=f"Failed {action}: {detail}")


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


def configure_polls_tools(mcp: MCPServer):
    """Configure Polls app MCP tools."""

    @mcp.tool(
        title="List Polls",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("polls.read")
    @instrument_tool
    async def nc_polls_list_polls(ctx: Context) -> ListPollsResponse:
        """List every poll the user owns or can access.

        Each entry carries the ID, title, type ('datePoll' for scheduling,
        'textPoll' for text answers), voting deadline, archived/expired flags
        and owner. Use the ID with the other nc_polls_* tools.
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
    @instrument_tool
    async def nc_polls_get_poll(poll_id: int, ctx: Context) -> GetPollResponse:
        """Get a single poll with its full configuration and status."""
        client = await get_client(ctx)
        with _polls_errors(f"getting poll {poll_id}"):
            data = await client.polls.get_poll(poll_id)
        return GetPollResponse(poll=Poll.from_api(data))

    @mcp.tool(
        title="Get Poll Options",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    @require_scopes("polls.read")
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
