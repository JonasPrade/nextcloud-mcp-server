"""Polls MCP tools, called directly against a stubbed client.

The client and models are covered in ``tests/client/polls``; these pin what the
tools add on top: the typed responses, the timezone they report, and that an
HTTP failure reaches the model as an ``MCPError`` naming the cause.
"""

from __future__ import annotations

import re

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from nextcloud_mcp_server.server.polls import configure_polls_tools
from tests.client.polls.test_polls_api import (
    POLL_FIXTURE,
    SLOT_SUMMER,
    SLOT_WINTER,
    TEXT_POLL_FIXTURE,
    _option,
    _vote,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def polls_tools():
    mcp = MCPServer("test")
    configure_polls_tools(mcp)
    return mcp._tool_manager


@pytest.fixture
def stub_client(mocker):
    client = mocker.MagicMock()
    client.polls.list_polls = mocker.AsyncMock(
        return_value=[POLL_FIXTURE, TEXT_POLL_FIXTURE]
    )
    client.polls.get_poll = mocker.AsyncMock(return_value=POLL_FIXTURE)
    # No options/votes in the details payload: the tools fall back to the
    # dedicated routes, which the tests below then exercise.
    client.polls.get_poll_details = mocker.AsyncMock(
        return_value={"poll": POLL_FIXTURE}
    )
    client.polls.get_options = mocker.AsyncMock(
        return_value=[
            _option(2, SLOT_WINTER, 2),
            _option(1, SLOT_SUMMER, 1),
            _option(3, SLOT_WINTER + 86400, 3, deleted=1790000000),
        ]
    )
    client.polls.get_votes = mocker.AsyncMock(
        return_value=[
            _vote(1, "yes", "alice", "Alice"),
            _vote(2, "yes", "alice", "Alice"),
            _vote(2, "yes", "bob", "Bob"),
        ]
    )
    mocker.patch(
        "nextcloud_mcp_server.server.polls.get_client",
        mocker.AsyncMock(return_value=client),
    )
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )
    return client


async def _call(polls_tools, mocker, name, **kwargs):
    return await polls_tools.get_tool(name).fn(ctx=mocker.MagicMock(), **kwargs)


async def test_list_polls(polls_tools, stub_client, mocker):
    result = await _call(polls_tools, mocker, "nc_polls_list_polls")

    assert result.total == 2
    assert [p.title for p in result.results] == ["Vorstandssitzung", "Ort"]
    assert result.results[1].expire is None


async def test_get_poll(polls_tools, stub_client, mocker):
    result = await _call(polls_tools, mocker, "nc_polls_get_poll", poll_id=7)

    assert result.poll.id == 7
    assert result.poll.configuration.title == "Vorstandssitzung"
    stub_client.polls.get_poll.assert_awaited_once_with(7)


async def test_get_options_in_poll_order_and_timezone(polls_tools, stub_client, mocker):
    result = await _call(polls_tools, mocker, "nc_polls_get_options", poll_id=7)

    assert result.timezone_name == "Europe/Berlin"
    assert result.poll_type == "datePoll"
    assert [o.id for o in result.results] == [1, 2]  # deleted option dropped
    assert result.results[0].start.isoformat() == "2026-10-20T18:00:00+02:00"
    assert result.results[0].end.isoformat() == "2026-10-20T20:00:00+02:00"


async def test_get_results_sorted_by_approval(polls_tools, stub_client, mocker):
    result = await _call(polls_tools, mocker, "nc_polls_get_results", poll_id=7)

    assert result.title == "Vorstandssitzung"
    assert [r.option_id for r in result.results] == [2, 1]
    assert result.results[0].yes_voters == ["Alice", "Bob"]
    assert result.participants == ["Alice", "Bob"]


async def test_results_use_one_request_when_details_carry_everything(
    polls_tools, stub_client, mocker
):
    """``/poll/{id}`` carries options and votes: no further round trips."""
    stub_client.polls.get_poll_details.return_value = {
        "poll": POLL_FIXTURE,
        "options": [_option(1, SLOT_SUMMER, 1)],
        "votes": [_vote(1, "maybe", "carol", "Carol")],
    }

    result = await _call(polls_tools, mocker, "nc_polls_get_results", poll_id=7)

    assert result.results[0].maybe_voters == ["Carol"]
    stub_client.polls.get_options.assert_not_awaited()
    stub_client.polls.get_votes.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (404, "poll not found, or not shared with you"),
        (403, "you do not have access to this poll"),
        (500, "server error (500)"),
    ],
)
async def test_http_errors_become_mcp_errors(
    polls_tools, stub_client, mocker, status, detail
):
    request = httpx.Request("GET", "https://nc.example/ocs/v2.php/apps/polls")
    stub_client.polls.get_poll.side_effect = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status, request=request)
    )

    with pytest.raises(MCPError, match=re.escape(detail)):
        await _call(polls_tools, mocker, "nc_polls_get_poll", poll_id=99)


def test_all_tools_are_read_only(polls_tools):
    tools = {t.name: t for t in polls_tools.list_tools()}

    assert set(tools) == {
        "nc_polls_list_polls",
        "nc_polls_get_poll",
        "nc_polls_get_options",
        "nc_polls_get_results",
    }
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is True
        assert tool.title
