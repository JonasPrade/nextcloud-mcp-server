"""Destructive calendar operations need ``calendar.delete``, not just ``calendar.write``.

``calendar.write`` creates and updates events; removing them is a separate grant
so a client (e.g. a scheduled routine) can maintain a calendar without being
able to empty it. Besides ``nc_calendar_delete_event`` the other deleting paths
are operations inside tools that otherwise only need ``calendar.write``.
"""

from __future__ import annotations

import pytest
from mcp.server.auth.middleware.auth_context import AccessToken
from mcp.server.mcpserver import MCPServer

from nextcloud_mcp_server.auth.scope_authorization import InsufficientScopeError
from nextcloud_mcp_server.server.calendar import configure_calendar_tools

pytestmark = pytest.mark.unit

WRITE_ONLY = ["openid", "calendar.read", "calendar.write"]
WITH_DELETE = [*WRITE_ONLY, "calendar.delete"]

DESTRUCTIVE_CALLS = [
    ("nc_calendar_bulk_operations", {"operation": "delete"}),
    ("nc_calendar_bulk_operations", {"operation": "move"}),
    ("nc_calendar_manage_calendar", {"action": "delete", "calendar_name": "x"}),
]


@pytest.fixture
def calendar_tools():
    mcp = MCPServer("test")
    configure_calendar_tools(mcp)
    return mcp._tool_manager


@pytest.fixture
def client(mocker):
    client = mocker.MagicMock()
    client.calendar.delete_calendar = mocker.AsyncMock(return_value={"success": True})
    client.calendar.delete_event = mocker.AsyncMock(
        return_value={"success": True, "status_code": 204}
    )
    client.calendar.update_calendar = mocker.AsyncMock(return_value={"success": True})
    mocker.patch(
        "nextcloud_mcp_server.server.calendar.get_client",
        mocker.AsyncMock(return_value=client),
    )
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_settings",
        return_value=mocker.MagicMock(
            enable_login_flow=False,
            enable_offline_access=False,
            oidc_resource_server_id=None,
        ),
    )
    return client


def _token_with(mocker, scopes: list[str]) -> None:
    mocker.patch(
        "nextcloud_mcp_server.auth.scope_authorization.get_access_token",
        return_value=AccessToken(token="t", client_id="c", scopes=scopes),
    )


def test_delete_event_requires_calendar_delete(calendar_tools):
    tool = calendar_tools.get_tool("nc_calendar_delete_event")
    assert tool.fn._required_scopes == ["calendar.delete"]


def test_create_and_update_event_stay_on_calendar_write(calendar_tools):
    for name in ("nc_calendar_create_event", "nc_calendar_update_event"):
        assert calendar_tools.get_tool(name).fn._required_scopes == ["calendar.write"]


@pytest.mark.parametrize(("tool_name", "kwargs"), DESTRUCTIVE_CALLS)
async def test_destructive_operation_refused_with_write_only(
    calendar_tools, client, mocker, tool_name, kwargs
):
    _token_with(mocker, WRITE_ONLY)

    with pytest.raises(InsufficientScopeError) as exc:
        await calendar_tools.get_tool(tool_name).fn(ctx=mocker.MagicMock(), **kwargs)

    assert exc.value.missing_scopes == ["calendar.delete"]
    client.calendar.delete_calendar.assert_not_called()
    client.calendar.delete_event.assert_not_called()


async def test_manage_calendar_delete_allowed_with_calendar_delete(
    calendar_tools, client, mocker
):
    _token_with(mocker, WITH_DELETE)

    await calendar_tools.get_tool("nc_calendar_manage_calendar").fn(
        ctx=mocker.MagicMock(), action="delete", calendar_name="x"
    )

    client.calendar.delete_calendar.assert_awaited_once_with("x")


async def test_manage_calendar_non_destructive_actions_need_only_write(
    calendar_tools, client, mocker
):
    _token_with(mocker, WRITE_ONLY)
    client.calendar.list_calendars = mocker.AsyncMock(return_value=[])

    result = await calendar_tools.get_tool("nc_calendar_manage_calendar").fn(
        ctx=mocker.MagicMock(), action="list"
    )

    assert result == []
