"""Tool-level behaviour of ``nc_calendar_create_event`` / ``nc_calendar_update_event``."""

from __future__ import annotations

import pytest
from httpx import Request, Response
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nextcloud_mcp_server.client.dav_errors import DavPreconditionFailed
from nextcloud_mcp_server.models.calendar import EventWriteResponse
from nextcloud_mcp_server.server.calendar import configure_calendar_tools

pytestmark = pytest.mark.unit

STORED_EVENT = {
    "uid": "uid-1",
    "title": "Messe",
    "start_datetime": "2025-11-13",
    "end_datetime": "2025-11-14",
    "all_day": True,
    "location": "Berlin",
    "etag": '"current"',
}


@pytest.fixture
def calendar_tools():
    mcp = MCPServer("test")
    configure_calendar_tools(mcp)
    return mcp._tool_manager


@pytest.fixture
def client(mocker):
    client = mocker.MagicMock()
    client.calendar.create_event = mocker.AsyncMock(
        return_value={
            "uid": "uid-1",
            "href": "/dav/uid-1.ics",
            "etag": '"e1"',
            "status_code": 201,
            "event": dict(STORED_EVENT),
        }
    )
    client.calendar.update_event = mocker.AsyncMock(
        return_value={
            "uid": "uid-1",
            "href": "/dav/uid-1.ics",
            "etag": '"e2"',
            "status_code": 200,
            "event": dict(STORED_EVENT, location="Hamburg"),
        }
    )
    client.calendar.get_event = mocker.AsyncMock(
        return_value=(dict(STORED_EVENT), '"current"')
    )
    mocker.patch(
        "nextcloud_mcp_server.server.calendar.get_client",
        mocker.AsyncMock(return_value=client),
    )
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(enable_login_flow=False),
    )
    return client


def _precondition_failed(message: str | None = None) -> DavPreconditionFailed:
    request = Request("PUT", "https://nc.example.com/dav/uid-1.ics")
    return DavPreconditionFailed(
        "412 Precondition Failed",
        request=request,
        response=Response(412, request=request),
        dav_message=message,
    )


async def test_create_returns_typed_response_with_uid_calendar_etag_and_fields(
    calendar_tools, client, mocker
):
    result = await calendar_tools.get_tool("nc_calendar_create_event").fn(
        calendar_name="Branchentermine",
        title="Messe",
        start_datetime="2025-11-13",
        end_datetime="2025-11-14",
        ctx=mocker.MagicMock(),
    )

    assert isinstance(result, EventWriteResponse)
    assert result.success is True
    assert result.uid == "uid-1"
    assert result.calendar_name == "Branchentermine"
    assert result.etag == '"e1"'
    assert result.event["title"] == "Messe"
    assert result.event["all_day"] is True


async def test_create_does_not_force_all_day_false(calendar_tools, client, mocker):
    """An unset ``all_day`` must reach the client as None so it can be derived."""
    await calendar_tools.get_tool("nc_calendar_create_event").fn(
        calendar_name="Branchentermine",
        title="Messe",
        start_datetime="2025-11-13",
        ctx=mocker.MagicMock(),
    )

    event_data = client.calendar.create_event.call_args.args[1]
    assert event_data["all_day"] is None


async def test_update_forwards_only_given_fields_and_etag(
    calendar_tools, client, mocker
):
    result = await calendar_tools.get_tool("nc_calendar_update_event").fn(
        calendar_name="Branchentermine",
        event_uid="uid-1",
        location="Hamburg",
        etag='"e1"',
        ctx=mocker.MagicMock(),
    )

    args = client.calendar.update_event.call_args.args
    assert args[:2] == ("Branchentermine", "uid-1")
    assert args[2] == {"location": "Hamburg"}
    assert args[3] == '"e1"'
    assert isinstance(result, EventWriteResponse)
    assert result.etag == '"e2"'
    assert result.event["location"] == "Hamburg"


async def test_update_etag_conflict_reports_current_state(
    calendar_tools, client, mocker
):
    client.calendar.update_event.side_effect = _precondition_failed()

    with pytest.raises(ToolError) as exc:
        await calendar_tools.get_tool("nc_calendar_update_event").fn(
            calendar_name="Branchentermine",
            event_uid="uid-1",
            title="neu",
            etag='"stale"',
            ctx=mocker.MagicMock(),
        )

    message = str(exc.value)
    assert "was not updated" in message
    assert "nothing was written" in message
    assert "'title': 'Messe'" in message
    assert "'start_datetime': '2025-11-13'" in message
    assert '"current"' in message
    assert "nc_calendar_get_event" in message


async def test_update_etag_conflict_survives_unreadable_current_state(
    calendar_tools, client, mocker
):
    client.calendar.update_event.side_effect = _precondition_failed("changed")
    client.calendar.get_event.side_effect = ValueError("gone")

    with pytest.raises(ToolError) as exc:
        await calendar_tools.get_tool("nc_calendar_update_event").fn(
            calendar_name="Branchentermine",
            event_uid="uid-1",
            title="neu",
            ctx=mocker.MagicMock(),
        )

    assert "changed" in str(exc.value)
    assert "Current state" not in str(exc.value)


def test_event_write_tools_have_typed_return_annotation(calendar_tools):
    for name in ("nc_calendar_create_event", "nc_calendar_update_event"):
        assert calendar_tools.get_tool(name).fn.__annotations__["return"] in (
            EventWriteResponse,
            "EventWriteResponse",
        )
