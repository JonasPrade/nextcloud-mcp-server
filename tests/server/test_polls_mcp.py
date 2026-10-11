"""End-to-end coverage of the Polls MCP tools against a real stack.

The read test sets its poll up through the Polls OCS API directly, so it does
not depend on the write tools; the write test then drives the whole organiser
flow through the tools themselves. Both clean up through the API.
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


async def _remove_poll(nc_client: NextcloudClient, poll_id: int) -> None:
    """Archive, then delete a test poll; never let a failed cleanup mask a result.

    Polls only deletes archived polls on some versions, hence both steps.
    """
    for method, path in (
        ("PUT", f"/poll/{poll_id}/archive/toggle"),
        ("DELETE", f"/poll/{poll_id}"),
    ):
        try:
            await _ocs(nc_client, method, path)
        except Exception:
            logger.warning("Poll cleanup %s %s failed", method, path)


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
        await _remove_poll(nc_client, poll_id)


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


async def test_mcp_polls_write_round_trip(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Create a date poll, add slots, vote, comment, confirm, close, reopen."""
    title = f"MCP Write Poll {uuid.uuid4().hex[:8]}"
    poll_id = None
    try:
        created = _payload(
            await nc_mcp_client.call_tool(
                "nc_polls_create_poll",
                {
                    "title": title,
                    "poll_type": "datePoll",
                    "description": "created by the MCP integration tests",
                    "allow_maybe": True,
                    "timezone_name": "Europe/Berlin",
                },
            )
        )
        poll_id = created["poll"]["id"]
        assert created["poll"]["configuration"]["title"] == title
        assert created["poll"]["configuration"]["allow_maybe"] is True

        # Verify via the direct client, not only through the tool that made it.
        direct = await nc_client.polls.get_poll(poll_id)
        assert direct["configuration"]["title"] == title

        first = _payload(
            await nc_mcp_client.call_tool(
                "nc_polls_add_date_option",
                {
                    "poll_id": poll_id,
                    "start": "2030-03-04T10:00:00+00:00",
                    "duration_minutes": 90,
                },
            )
        )
        second = _payload(
            await nc_mcp_client.call_tool(
                "nc_polls_add_date_option",
                {
                    "poll_id": poll_id,
                    "start": "2030-03-05T10:00:00+00:00",
                    "end": "2030-03-05T11:00:00+00:00",
                },
            )
        )
        first_id = first["added"][0]["id"]
        second_id = second["added"][0]["id"]
        options = await nc_client.polls.get_options(poll_id)
        by_id = {o["id"]: o for o in options}
        assert by_id[first_id]["timestamp"] == SLOT_A
        assert by_id[first_id]["duration"] == 5400
        assert by_id[second_id]["duration"] == 3600

        for option_id, answer in ((first_id, "maybe"), (second_id, "yes")):
            voted = _payload(
                await nc_mcp_client.call_tool(
                    "nc_polls_vote",
                    {"poll_id": poll_id, "option_id": option_id, "answer": answer},
                )
            )
            assert voted["answer"] == answer

        commented = _payload(
            await nc_mcp_client.call_tool(
                "nc_polls_add_comment",
                {"poll_id": poll_id, "comment": "Dienstag passt besser"},
            )
        )
        assert commented["comment"]["comment"] == "Dienstag passt besser"
        comments = _payload(
            await nc_mcp_client.call_tool("nc_polls_get_comments", {"poll_id": poll_id})
        )
        assert [c["comment"] for c in comments["results"]] == ["Dienstag passt besser"]

        results = _payload(
            await nc_mcp_client.call_tool("nc_polls_get_results", {"poll_id": poll_id})
        )
        assert [r["option_id"] for r in results["results"]] == [second_id, first_id]
        assert results["results"][1]["maybe"] == 1

        closed = _payload(
            await nc_mcp_client.call_tool("nc_polls_close_poll", {"poll_id": poll_id})
        )
        # Not asserting is_expired: Polls closes by setting the deadline to
        # "now", and whether that already reads as expired is a matter of
        # the same second.
        assert closed["poll"]["id"] == poll_id

        # Confirming twice leaves it confirmed: the tool is a setter, not a toggle.
        for expected_change in (True, False):
            confirmed = _payload(
                await nc_mcp_client.call_tool(
                    "nc_polls_confirm_option",
                    {"poll_id": poll_id, "option_id": second_id, "confirmed": True},
                )
            )
            assert confirmed["changed"] is expected_change
            assert confirmed["option"]["confirmed"] is True

        reopened = _payload(
            await nc_mcp_client.call_tool("nc_polls_reopen_poll", {"poll_id": poll_id})
        )
        assert reopened["poll"]["id"] == poll_id

        updated = _payload(
            await nc_mcp_client.call_tool(
                "nc_polls_update_poll",
                {"poll_id": poll_id, "title": f"{title} (renamed)"},
            )
        )
        assert updated["poll"]["configuration"]["title"] == f"{title} (renamed)"
    finally:
        if poll_id is not None:
            await _remove_poll(nc_client, poll_id)
