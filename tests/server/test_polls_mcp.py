"""End-to-end coverage of the read-only Polls MCP tools against a real stack.

The MCP server has no tools to write polls yet, so the fixture sets one up
through the Polls OCS API directly: a date poll with two slots and one vote.
"""

import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.client.ocs import OCS_REQUEST_HEADERS

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration

API = "/ocs/v2.php/apps/polls/api/v1.0"

# Two one-hour slots, far enough in the future that Polls never treats them as
# past. 2030-03-04 10:00 UTC and 2030-03-05 10:00 UTC.
SLOT_A = 1899194400
SLOT_B = SLOT_A + 86400


def _payload(result):
    assert result.is_error is False, f"MCP tool call failed: {result.content}"
    return json.loads(result.content[0].text)


async def _ocs(nc_client: NextcloudClient, method: str, path: str, **kwargs) -> Any:
    response = await nc_client._client.request(
        method,
        f"{API}{path}",
        headers=dict(OCS_REQUEST_HEADERS),
        params={"format": "json"},
        **kwargs,
    )
    response.raise_for_status()
    return response.json()["ocs"]["data"]


@pytest.fixture
async def temporary_poll(
    nc_client: NextcloudClient,
) -> AsyncGenerator[dict[str, Any], Any]:
    """A date poll with two slots and a 'yes' vote by the test user on slot B."""
    title = f"MCP Test Poll {uuid.uuid4().hex[:8]}"
    created = await _ocs(
        nc_client, "POST", "/poll", json={"type": "datePoll", "title": title}
    )
    poll_id = created["poll"]["id"]
    try:
        option_ids = {}
        for slot in (SLOT_A, SLOT_B):
            added = await _ocs(
                nc_client,
                "POST",
                f"/poll/{poll_id}/option",
                json={"option": {"timestamp": slot, "duration": 3600}},
            )
            option_ids[slot] = added["option"]["id"]
        await _ocs(nc_client, "PUT", f"/option/{option_ids[SLOT_B]}/vote/yes")
        yield {"id": poll_id, "title": title, "option_ids": option_ids}
    finally:
        # Polls only deletes archived polls on some versions; archive first and
        # keep going if either step fails, so a broken cleanup never hides the
        # test result.
        for method, path in (
            ("PUT", f"/poll/{poll_id}/archive/toggle"),
            ("DELETE", f"/poll/{poll_id}"),
        ):
            try:
                await _ocs(nc_client, method, path)
            except Exception:
                logger.warning("Poll cleanup %s %s failed", method, path)


async def test_mcp_polls_read_round_trip(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
    temporary_poll: dict[str, Any],
):
    poll_id = temporary_poll["id"]
    slot_b_id = temporary_poll["option_ids"][SLOT_B]

    listed = _payload(await nc_mcp_client.call_tool("nc_polls_list_polls", {}))
    entry = next(p for p in listed["results"] if p["id"] == poll_id)
    assert entry["title"] == temporary_poll["title"]
    assert entry["type"] == "datePoll"
    assert entry["is_archived"] is False

    poll = _payload(
        await nc_mcp_client.call_tool("nc_polls_get_poll", {"poll_id": poll_id})
    )
    assert poll["poll"]["configuration"]["title"] == temporary_poll["title"]

    options = _payload(
        await nc_mcp_client.call_tool("nc_polls_get_options", {"poll_id": poll_id})
    )
    assert options["total"] == 2
    assert [o["id"] for o in options["results"]] == list(
        temporary_poll["option_ids"].values()
    )
    # Start/end are rendered with an explicit offset, so they are unambiguous
    # whatever timezone the poll was created in.
    first = options["results"][0]
    assert first["start"] is not None and first["end"] is not None
    assert first["start"] != first["end"]

    results = _payload(
        await nc_mcp_client.call_tool("nc_polls_get_results", {"poll_id": poll_id})
    )
    assert results["results"][0]["option_id"] == slot_b_id
    assert results["results"][0]["yes"] == 1
    assert len(results["results"][0]["yes_voters"]) == 1
    assert results["participants"] == results["results"][0]["yes_voters"]

    # Cross-check against the direct client, not only through the tools.
    votes = await nc_client.polls.get_votes(poll_id)
    assert [v["optionId"] for v in votes if v["answer"] == "yes"] == [slot_b_id]


async def test_mcp_polls_unknown_poll_is_an_error(nc_mcp_client: ClientSession):
    result = await nc_mcp_client.call_tool("nc_polls_get_poll", {"poll_id": 999999999})
    assert result.is_error is True
