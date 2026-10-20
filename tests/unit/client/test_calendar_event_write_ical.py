"""iCalendar produced by ``nc_calendar_create_event`` / ``nc_calendar_update_event``.

Pins the wire format the Branchentermine routine depends on: all-day events use
``VALUE=DATE`` with an exclusive ``DTEND``, timed events carry a ``TZID``,
text values are escaped, UTF-8 survives, and an update bumps ``SEQUENCE``.
"""

from __future__ import annotations

import datetime as dt
import re

import httpx
import pytest
from icalendar import Calendar

from nextcloud_mcp_server.client.calendar import CalendarClient

pytestmark = pytest.mark.unit


def _make_client(mocker) -> CalendarClient:
    client = CalendarClient.__new__(CalendarClient)
    client._client = mocker.AsyncMock(spec=httpx.AsyncClient)
    client._username = "tester"
    return client


def _unfold(ical: str) -> str:
    """Undo RFC 5545 line folding so assertions can match whole property lines."""
    return re.sub(r"\r?\n[ \t]", "", ical)


def _create(mocker, **event_data) -> str:
    client = _make_client(mocker)
    data = client._with_inferred_all_day(event_data)
    return _unfold(client._create_ical_event(data, event_uid="uid-1"))


def _vevent(ical: str):
    cal = Calendar.from_ical(ical)
    return next(c for c in cal.walk() if c.name == "VEVENT")


# ============= All-day =============


def test_all_day_single_day(mocker):
    """One day on 13 Nov: DTEND is the 14th (exclusive)."""
    ical = _create(
        mocker, title="Messe", start_datetime="2025-11-13", end_datetime="2025-11-14"
    )

    assert "DTSTART;VALUE=DATE:20251113" in ical
    assert "DTEND;VALUE=DATE:20251114" in ical
    assert "TZID" not in ical


def test_all_day_derived_from_date_only_start_without_end(mocker):
    """No ``all_day`` flag and no end: a date-only start means a one-day event."""
    event = _vevent(_create(mocker, title="Messe", start_datetime="2025-11-13"))

    assert event["DTSTART"].dt == dt.date(2025, 11, 13)
    assert type(event["DTSTART"].dt) is dt.date


def test_all_day_multi_day(mocker):
    """12–13 Nov is DTSTART 12th, DTEND 14th."""
    ical = _create(
        mocker, title="Kongress", start_datetime="2025-11-12", end_datetime="2025-11-14"
    )

    assert "DTSTART;VALUE=DATE:20251112" in ical
    assert "DTEND;VALUE=DATE:20251114" in ical


def test_all_day_inclusive_end_is_moved_to_next_day(mocker):
    """A caller who passes end == start must not produce a zero-length range."""
    ical = _create(
        mocker, title="Messe", start_datetime="2025-11-13", end_datetime="2025-11-13"
    )

    assert "DTSTART;VALUE=DATE:20251113" in ical
    assert "DTEND;VALUE=DATE:20251114" in ical


def test_explicit_all_day_false_keeps_a_timed_event(mocker):
    ical = _create(
        mocker,
        title="x",
        start_datetime="2025-11-13T09:00:00",
        end_datetime="2025-11-13T10:00:00",
        all_day=False,
    )

    assert "VALUE=DATE" not in ical


# ============= Timed =============


def test_timed_defaults_to_europe_berlin(mocker):
    ical = _create(
        mocker,
        title="Sitzung",
        start_datetime="2025-11-13T09:30:00",
        end_datetime="2025-11-13T11:00:00",
    )

    assert "DTSTART;TZID=Europe/Berlin:20251113T093000" in ical
    assert "DTEND;TZID=Europe/Berlin:20251113T110000" in ical
    assert "BEGIN:VTIMEZONE" in ical


def test_timed_with_explicit_timezone(mocker):
    ical = _create(
        mocker,
        title="x",
        start_datetime="2025-11-13T09:30:00",
        end_datetime="2025-11-13T11:00:00",
        timezone="Europe/London",
    )

    assert "DTSTART;TZID=Europe/London:20251113T093000" in ical
    assert "Europe/Berlin" not in ical


# ============= Metadata =============


def test_server_side_metadata_is_set(mocker):
    event = _vevent(_create(mocker, title="x", start_datetime="2025-11-13"))

    assert str(event["UID"]) == "uid-1"
    for prop in ("DTSTAMP", "CREATED", "LAST-MODIFIED"):
        assert event[prop].dt.tzinfo is not None, prop
    assert event["SEQUENCE"] == 0


def test_optional_fields_absent_when_not_given(mocker):
    ical = _create(mocker, title="x", start_datetime="2025-11-13")

    assert "URL" not in ical
    assert "CATEGORIES" not in ical


def test_optional_fields_present_when_given(mocker):
    event = _vevent(
        _create(
            mocker,
            title="Messe",
            start_datetime="2025-11-13",
            location="Berlin, Halle 3",
            description="Zeile 1\nZeile 2",
            url="https://example.org/messe",
            categories="Messe,Branche",
        )
    )

    assert str(event["LOCATION"]) == "Berlin, Halle 3"
    assert str(event["DESCRIPTION"]) == "Zeile 1\nZeile 2"
    assert str(event["URL"]) == "https://example.org/messe"
    assert [str(c) for c in event["CATEGORIES"].cats] == ["Messe", "Branche"]


# ============= Escaping and UTF-8 =============


def test_text_values_are_escaped_on_the_wire(mocker):
    ical = _create(
        mocker,
        title="A; B, C",
        start_datetime="2025-11-13",
        location="Halle 3, Berlin; Eingang Nord",
        description="Zeile 1\nZeile 2, mit Komma; und Semikolon",
        categories="Messe,Branche",
    )

    assert "SUMMARY:A\; B\\, C" in ical
    assert "LOCATION:Halle 3\\, Berlin\; Eingang Nord" in ical
    assert "DESCRIPTION:Zeile 1\\nZeile 2\\, mit Komma\; und Semikolon" in ical
    assert "CATEGORIES:Messe,Branche" in ical


def test_escaped_text_roundtrips_through_the_parser(mocker):
    client = _make_client(mocker)
    ical = client._create_ical_event(
        {
            "title": "A; B, C",
            "start_datetime": "2025-11-13",
            "all_day": True,
            "description": "x\ny, z; w",
        },
        event_uid="uid-1",
    )

    parsed = client._parse_ical_event(ical)

    assert parsed["title"] == "A; B, C"
    assert parsed["description"] == "x\ny, z; w"


def test_umlauts_and_dashes_survive_utf8(mocker):
    client = _make_client(mocker)
    title = "Verkehrsausschuss – Anhörung zum Deutschlandtakt (Größe ä ö ü ß)"
    ical = client._create_ical_event(
        {"title": title, "start_datetime": "2025-11-13", "all_day": True},
        event_uid="uid-1",
    )

    assert client._parse_ical_event(ical)["title"] == title
    assert title.encode("utf-8") in _unfold(ical).encode("utf-8")


# ============= Update: partial fields, SEQUENCE =============

STORED = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n"
    "BEGIN:VEVENT\r\nUID:uid-1\r\nSUMMARY:Messe\r\n"
    "DTSTART;VALUE=DATE:20251113\r\nDTEND;VALUE=DATE:20251114\r\n"
    "LOCATION:Berlin\r\nDESCRIPTION:Alt\r\nURL:https://example.org/alt\r\n"
    "CATEGORIES:Messe\r\nSEQUENCE:2\r\n"
    "DTSTAMP:20250101T000000Z\r\nLAST-MODIFIED:20250101T000000Z\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def _merge(mocker, **event_data) -> str:
    client = _make_client(mocker)
    data = client._with_inferred_all_day(event_data)
    return client._merge_ical_properties(STORED, data)


def test_update_changes_only_given_fields(mocker):
    event = _vevent(_merge(mocker, location="Hamburg"))

    assert str(event["LOCATION"]) == "Hamburg"
    assert str(event["SUMMARY"]) == "Messe"
    assert str(event["DESCRIPTION"]) == "Alt"
    assert str(event["URL"]) == "https://example.org/alt"
    assert event["DTSTART"].dt == dt.date(2025, 11, 13)
    assert event["DTEND"].dt == dt.date(2025, 11, 14)


def test_update_bumps_sequence_and_last_modified(mocker):
    event = _vevent(_merge(mocker, title="Messe (verschoben)"))

    assert event["SEQUENCE"] == 3
    assert event["LAST-MODIFIED"].dt > dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    assert event["DTSTAMP"].dt > dt.datetime(2025, 1, 1, tzinfo=dt.UTC)


def test_update_starts_sequence_at_one_when_absent(mocker):
    client = _make_client(mocker)
    stored = STORED.replace("SEQUENCE:2\r\n", "")

    event = _vevent(client._merge_ical_properties(stored, {"title": "x"}))

    assert event["SEQUENCE"] == 1


def test_update_reschedules_all_day_event_keeping_it_all_day(mocker):
    ical = _unfold(
        _merge(mocker, start_datetime="2025-11-20", end_datetime="2025-11-22")
    )

    assert "DTSTART;VALUE=DATE:20251120" in ical
    assert "DTEND;VALUE=DATE:20251122" in ical
    assert "TZID" not in ical


def test_update_clears_categories_with_empty_string(mocker):
    event = _vevent(_merge(mocker, categories=""))

    assert "CATEGORIES" not in event
