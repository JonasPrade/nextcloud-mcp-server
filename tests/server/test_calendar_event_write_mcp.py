"""Integration: create, read, update and re-read events through the MCP tools.

Covers the Branchentermine flow end to end: an all-day multi-day event is
created, read back with ``nc_calendar_get_event``, rescheduled with a partial
update, and read again. Also checks that a stale ETag is refused with the
event's current state.
"""

import json

import pytest
from mcp import ClientSession

pytestmark = pytest.mark.integration


async def _call(client: ClientSession, tool: str, args: dict) -> dict:
    result = await client.call_tool(tool, args)
    assert result.is_error is False, f"{tool} failed: {result.content}"
    return json.loads(result.content[0].text)


async def test_mcp_create_read_update_all_day_event(
    nc_mcp_client: ClientSession, temporary_calendar: str
):
    created = await _call(
        nc_mcp_client,
        "nc_calendar_create_event",
        {
            "calendar_name": temporary_calendar,
            "title": "Verkehrsausschuss – Anhörung, Teil 1; öffentlich",
            "start_datetime": "2026-11-12",
            "end_datetime": "2026-11-14",
            "location": "Berlin, Halle 3",
            "description": "Zeile 1\nZeile 2",
            "url": "https://example.org/termin",
            "categories": "Branche,Anhörung",
        },
    )
    uid = created["uid"]
    assert created["calendar_name"] == temporary_calendar
    assert created["etag"]

    event = await _call(
        nc_mcp_client,
        "nc_calendar_get_event",
        {"calendar_name": temporary_calendar, "event_uid": uid},
    )
    assert event["title"] == "Verkehrsausschuss – Anhörung, Teil 1; öffentlich"
    assert event["all_day"] is True
    assert event["start_datetime"] == "2026-11-12"
    assert event["end_datetime"] == "2026-11-14"
    assert event["location"] == "Berlin, Halle 3"
    assert event["description"] == "Zeile 1\nZeile 2"
    assert event["url"] == "https://example.org/termin"
    assert "Branche" in event["categories"]

    updated = await _call(
        nc_mcp_client,
        "nc_calendar_update_event",
        {
            "calendar_name": temporary_calendar,
            "event_uid": uid,
            "start_datetime": "2026-11-19",
            "end_datetime": "2026-11-21",
            "etag": created["etag"],
        },
    )
    assert updated["uid"] == uid
    assert updated["etag"] != created["etag"]

    reread = await _call(
        nc_mcp_client,
        "nc_calendar_get_event",
        {"calendar_name": temporary_calendar, "event_uid": uid},
    )
    assert reread["start_datetime"] == "2026-11-19"
    assert reread["end_datetime"] == "2026-11-21"
    assert reread["all_day"] is True
    # Untouched fields are preserved.
    assert reread["title"] == event["title"]
    assert reread["location"] == "Berlin, Halle 3"
    assert reread["url"] == "https://example.org/termin"


async def test_mcp_create_timed_event_defaults_to_berlin(
    nc_mcp_client: ClientSession, temporary_calendar: str
):
    created = await _call(
        nc_mcp_client,
        "nc_calendar_create_event",
        {
            "calendar_name": temporary_calendar,
            "title": "Sitzung",
            "start_datetime": "2026-11-12T09:30:00",
            "end_datetime": "2026-11-12T11:00:00",
        },
    )

    event = await _call(
        nc_mcp_client,
        "nc_calendar_get_event",
        {"calendar_name": temporary_calendar, "event_uid": created["uid"]},
    )
    assert event["all_day"] is False
    assert event["start_tz"] == "Europe/Berlin"
    assert event["start_datetime"].startswith("2026-11-12T09:30:00")


async def test_mcp_update_with_stale_etag_is_refused(
    nc_mcp_client: ClientSession, temporary_calendar: str
):
    created = await _call(
        nc_mcp_client,
        "nc_calendar_create_event",
        {
            "calendar_name": temporary_calendar,
            "title": "Original",
            "start_datetime": "2026-11-12",
        },
    )
    await _call(
        nc_mcp_client,
        "nc_calendar_update_event",
        {
            "calendar_name": temporary_calendar,
            "event_uid": created["uid"],
            "title": "Von jemand anderem geändert",
        },
    )

    result = await nc_mcp_client.call_tool(
        "nc_calendar_update_event",
        {
            "calendar_name": temporary_calendar,
            "event_uid": created["uid"],
            "title": "Mein Update",
            "etag": created["etag"],
        },
    )

    assert result.is_error is True
    text = result.content[0].text
    assert "nothing was written" in text
    assert "Von jemand anderem geändert" in text
