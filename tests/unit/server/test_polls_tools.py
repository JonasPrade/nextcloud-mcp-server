"""Polls MCP tools, called directly against a stubbed client.

The client and models are covered in ``tests/client/polls``; these pin what the
tools add on top: the typed responses, the timezone they report, what the write
tools send to Polls (and what they refuse to send), the link to the poll, and
that an HTTP failure reaches the model as an ``MCPError`` naming the cause.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from nextcloud_mcp_server.server.polls import configure_polls_tools
from tests.client.polls.test_polls_api import (
    BERLIN,
    POLL_FIXTURE,
    SLOT_SUMMER,
    SLOT_WINTER,
    TEXT_POLL_FIXTURE,
    _option,
    _user,
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
    """The list maps each poll and turns a 0 deadline into None."""
    result = await _call(polls_tools, mocker, "nc_polls_list_polls")

    assert result.total == 2
    assert [p.title for p in result.results] == ["Vorstandssitzung", "Ort"]
    assert result.results[1].expire is None


async def test_get_poll(polls_tools, stub_client, mocker):
    """A single poll comes back with its configuration."""
    result = await _call(polls_tools, mocker, "nc_polls_get_poll", poll_id=7)

    assert result.poll.id == 7
    assert result.poll.configuration.title == "Vorstandssitzung"
    stub_client.polls.get_poll.assert_awaited_once_with(7)


async def test_get_options_in_poll_order_and_timezone(polls_tools, stub_client, mocker):
    """Options sort by poll order, deleted ones drop, times are local."""
    result = await _call(polls_tools, mocker, "nc_polls_get_options", poll_id=7)

    assert result.timezone_name == "Europe/Berlin"
    assert result.poll_type == "datePoll"
    assert [o.id for o in result.results] == [1, 2]
    assert result.results[0].start.isoformat() == "2026-10-20T18:00:00+02:00"
    assert result.results[0].end.isoformat() == "2026-10-20T20:00:00+02:00"


async def test_get_results_sorted_by_approval(polls_tools, stub_client, mocker):
    """The tally lists the most-approved option first."""
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


def _comment(comment_id, text, timestamp, deleted=0):
    return {
        "id": comment_id,
        "pollId": 7,
        "timestamp": timestamp,
        "comment": text,
        "confidential": 0,
        "parent": 0,
        "deleted": deleted,
        "user": {"id": "alice", "userId": "alice", "displayName": "Alice"},
        "recipient": None,
    }


async def test_get_comments_oldest_first_without_deleted(
    polls_tools, stub_client, mocker
):
    """Comments sort by time and deleted ones are left out."""
    stub_client.polls.get_comments = mocker.AsyncMock(
        return_value=[
            _comment(2, "später", 1790000200),
            _comment(1, "zuerst", 1790000100),
            _comment(3, "weg", 1790000300, deleted=1790000400),
        ]
    )

    result = await _call(polls_tools, mocker, "nc_polls_get_comments", poll_id=7)

    assert [c.comment for c in result.results] == ["zuerst", "später"]
    assert result.results[0].author == "Alice"
    assert result.total == 2


# --- Write tools ---


def _created(owner_zone=None):
    """A POST /poll answer as Polls 9.3 gives it.

    ``timezoneName`` is "simple" (the voting variant, see Polls' add()) and the
    configuration is stale; ``owner.timeZone`` is the user's own zone.
    """
    owner = _user("alice", "Alice A.")
    if owner_zone is not None:
        owner["timeZone"] = owner_zone
    return {
        **POLL_FIXTURE,
        "id": 9,
        "configuration": {
            **POLL_FIXTURE["configuration"],
            "timezoneName": "simple",
            "allowMaybe": False,
        },
        "owner": owner,
    }


async def test_create_poll_with_settings(polls_tools, stub_client, mocker):
    """Settings go out in one follow-up update; the poll comes from its answer."""
    stub_client.polls.create_poll = mocker.AsyncMock(
        return_value=_created("America/New_York")
    )
    stored = {**POLL_FIXTURE, "id": 9}
    stub_client.polls.update_poll = mocker.AsyncMock(return_value=stored)

    result = await _call(
        polls_tools,
        mocker,
        "nc_polls_create_poll",
        title="Vorstandssitzung",
        poll_type="datePoll",
        description="Bitte abstimmen",
        expire="2026-10-31T18:00",
        allow_maybe=True,
        access="open",
        timezone_name="Europe/Berlin",
    )

    stub_client.polls.create_poll.assert_awaited_once_with(
        "Vorstandssitzung", "datePoll"
    )
    # The explicit zone wins over the owner's.
    stub_client.polls.update_poll.assert_awaited_once_with(
        9,
        {
            "description": "Bitte abstimmen",
            "expire": int(datetime(2026, 10, 31, 18, 0, tzinfo=BERLIN).timestamp()),
            "allowMaybe": True,
            "access": "open",
            "timezoneName": "Europe/Berlin",
        },
    )
    # Not the stale POST answer (allowMaybe False, timezoneName "simple").
    assert result.poll.configuration.allow_maybe is True
    assert result.poll.configuration.timezone_name == "Europe/Berlin"


@pytest.mark.parametrize(
    ("owner_zone", "expected"),
    [
        ("Europe/Berlin", "Europe/Berlin"),
        (None, "UTC"),
        ("", "UTC"),
        ("Mars/Olympus", "UTC"),
    ],
)
async def test_create_poll_always_sets_the_timezone(
    polls_tools, stub_client, mocker, owner_zone, expected
):
    """Without timezone_name the owner's zone is sent, or UTC if it is unusable.

    Even title and type alone need the update: Polls 9.3 stores the voting
    variant as timezoneName.
    """
    stub_client.polls.create_poll = mocker.AsyncMock(return_value=_created(owner_zone))
    stub_client.polls.update_poll = mocker.AsyncMock(
        return_value={**POLL_FIXTURE, "id": 9}
    )

    await _call(
        polls_tools, mocker, "nc_polls_create_poll", title="T", poll_type="datePoll"
    )

    stub_client.polls.update_poll.assert_awaited_once_with(
        9, {"timezoneName": expected}
    )


@pytest.mark.parametrize(
    ("owner_zone", "tz"),
    [("Europe/Berlin", BERLIN), ("Mars/Olympus", timezone.utc)],
)
async def test_create_poll_reads_expire_in_the_resolved_zone(
    polls_tools, stub_client, mocker, owner_zone, tz
):
    """A deadline without offset is local to the zone the poll ends up in."""
    stub_client.polls.create_poll = mocker.AsyncMock(return_value=_created(owner_zone))
    stub_client.polls.update_poll = mocker.AsyncMock(
        return_value={**POLL_FIXTURE, "id": 9}
    )

    await _call(
        polls_tools,
        mocker,
        "nc_polls_create_poll",
        title="T",
        poll_type="datePoll",
        expire="2026-10-31T18:00",
    )

    sent = stub_client.polls.update_poll.await_args.args[1]
    assert sent["expire"] == int(datetime(2026, 10, 31, 18, 0, tzinfo=tz).timestamp())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"title": "  "}, "title is empty"),
        ({"title": "T", "timezone_name": "Mars/Base"}, "unknown timezone"),
        ({"title": "T", "expire": "next friday"}, "ISO 8601"),
    ],
)
async def test_create_poll_rejects_bad_input_before_creating(
    polls_tools, stub_client, mocker, kwargs, message
):
    """Invalid input never leaves a half-configured poll behind."""
    stub_client.polls.create_poll = mocker.AsyncMock()

    with pytest.raises(MCPError, match=re.escape(message)):
        await _call(
            polls_tools, mocker, "nc_polls_create_poll", poll_type="datePoll", **kwargs
        )

    stub_client.polls.create_poll.assert_not_awaited()


async def test_update_poll_sends_only_given_settings(polls_tools, stub_client, mocker):
    """Untouched settings are not sent, so they keep their value."""
    stub_client.polls.update_poll = mocker.AsyncMock(return_value=POLL_FIXTURE)

    await _call(
        polls_tools,
        mocker,
        "nc_polls_update_poll",
        poll_id=7,
        title="Neu",
        remove_expire=True,
    )

    stub_client.polls.update_poll.assert_awaited_once_with(
        7, {"title": "Neu", "expire": 0}
    )


async def test_update_poll_reads_expire_in_the_polls_timezone(
    polls_tools, stub_client, mocker
):
    """A deadline without offset means local time of the poll."""
    stub_client.polls.update_poll = mocker.AsyncMock(return_value=POLL_FIXTURE)

    await _call(
        polls_tools,
        mocker,
        "nc_polls_update_poll",
        poll_id=7,
        expire="2026-12-01T12:00",
    )

    sent = stub_client.polls.update_poll.await_args.args[1]
    assert sent == {
        "expire": int(datetime(2026, 12, 1, 12, 0, tzinfo=BERLIN).timestamp())
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "nothing to change"),
        ({"expire": "2026-12-01T12:00", "remove_expire": True}, "not both"),
    ],
)
async def test_update_poll_rejects_empty_or_conflicting_changes(
    polls_tools, stub_client, mocker, kwargs, message
):
    """No request goes out for an update that cannot mean anything."""
    stub_client.polls.update_poll = mocker.AsyncMock()

    with pytest.raises(MCPError, match=message):
        await _call(polls_tools, mocker, "nc_polls_update_poll", poll_id=7, **kwargs)

    stub_client.polls.update_poll.assert_not_awaited()


@pytest.mark.parametrize(
    ("kwargs", "iso_timestamp", "iso_duration"),
    [
        # Summer time: +02:00, end becomes a duration.
        (
            {"start": "2026-10-23T18:00", "end": "2026-10-23T20:00"},
            "2026-10-23T18:00:00+02:00",
            "PT2H",
        ),
        # After the DST switch: +01:00.
        (
            {"start": "2026-10-27T18:00", "duration_minutes": 90},
            "2026-10-27T18:00:00+01:00",
            "PT1H30M",
        ),
        # A point in time: no duration at all.
        ({"start": "2026-10-23T18:00"}, "2026-10-23T18:00:00+02:00", None),
        # An explicit offset is honoured, then expressed in the poll's zone.
        (
            {"start": "2026-10-20T16:00:00+00:00", "duration_minutes": 60},
            "2026-10-20T18:00:00+02:00",
            "PT1H",
        ),
        # end across the DST switch: 25 wall-clock hours are 26 real ones.
        (
            {"start": "2026-10-24T18:00", "end": "2026-10-25T19:00"},
            "2026-10-24T18:00:00+02:00",
            "P1DT2H",
        ),
    ],
)
async def test_add_date_option_sends_iso_in_the_polls_zone(
    polls_tools, stub_client, mocker, kwargs, iso_timestamp, iso_duration
):
    """Start goes out as ISO 8601 in the poll's zone, the length as ISO duration."""
    stub_client.polls.add_option = mocker.AsyncMock(
        return_value=_option(5, SLOT_SUMMER, 3, duration=5400)
    )

    await _call(polls_tools, mocker, "nc_polls_add_date_option", poll_id=7, **kwargs)

    stub_client.polls.add_option.assert_awaited_once_with(
        7, iso_timestamp, iso_duration
    )


async def test_add_date_option_reports_the_stored_slot(
    polls_tools, stub_client, mocker
):
    """start and end in the answer come from Polls' timestamp and duration."""
    stub_client.polls.add_option = mocker.AsyncMock(
        return_value=_option(5, SLOT_SUMMER, 3, duration=5400)
    )

    result = await _call(
        polls_tools,
        mocker,
        "nc_polls_add_date_option",
        poll_id=7,
        start="2026-10-20T18:00",
        end="2026-10-20T19:30",
    )

    assert result.note is None
    assert [o.id for o in result.added] == [5]
    assert result.added[0].start.isoformat() == "2026-10-20T18:00:00+02:00"
    assert result.added[0].end.isoformat() == "2026-10-20T19:30:00+02:00"


async def test_add_date_option_existing_slot_is_not_reported_as_added(
    polls_tools, stub_client, mocker
):
    """Polls answers 201 with the existing option for a duplicate slot."""
    stub_client.polls.add_option = mocker.AsyncMock(
        return_value=_option(1, SLOT_SUMMER, 1)
    )

    result = await _call(
        polls_tools,
        mocker,
        "nc_polls_add_date_option",
        poll_id=7,
        start="2026-10-20T18:00",
        duration_minutes=120,
    )

    assert result.added == []
    assert "already has this slot as option 1" in result.note


@pytest.mark.parametrize(
    ("kwargs", "iso_timestamp", "iso_duration"),
    [
        ({"start": "2026-10-30"}, "2026-10-30T00:00:00+01:00", "PT24H"),
        # Over the DST switch on 25 October: 49 hours, not P2D.
        (
            {"start": "2026-10-24", "end": "2026-10-25"},
            "2026-10-24T00:00:00+02:00",
            "PT49H",
        ),
        ({"start": "2026-10-25"}, "2026-10-25T00:00:00+02:00", "PT25H"),
    ],
)
async def test_add_all_day_option(
    polls_tools, stub_client, mocker, kwargs, iso_timestamp, iso_duration
):
    """All-day slots run from midnight to midnight after the last day."""
    stub_client.polls.add_option = mocker.AsyncMock(
        return_value=_option(5, SLOT_SUMMER, 3, duration=86400)
    )

    await _call(
        polls_tools,
        mocker,
        "nc_polls_add_date_option",
        poll_id=7,
        all_day=True,
        **kwargs,
    )

    stub_client.polls.add_option.assert_awaited_once_with(
        7, iso_timestamp, iso_duration
    )


@pytest.mark.parametrize(
    ("poll", "kwargs", "message"),
    [
        (
            POLL_FIXTURE,
            {"start": "2026-10-20T18:00", "end": "2026-10-20T17:00"},
            "ends before it starts",
        ),
        (
            POLL_FIXTURE,
            {
                "start": "2026-10-20T18:00",
                "end": "2026-10-20T19:00",
                "duration_minutes": 60,
            },
            "not both",
        ),
        (TEXT_POLL_FIXTURE, {"start": "2026-10-20T18:00"}, "not a date poll"),
        (
            POLL_FIXTURE,
            {"start": "2026-10-30", "all_day": True, "duration_minutes": 60},
            "does not apply to all_day",
        ),
        (
            POLL_FIXTURE,
            {"start": "2026-10-30T18:00", "all_day": True},
            "must be dates",
        ),
        (
            POLL_FIXTURE,
            {"start": "2026-10-30", "end": "2026-10-29", "all_day": True},
            "ends before it starts",
        ),
    ],
)
async def test_add_date_option_rejects_bad_input(
    polls_tools, stub_client, mocker, poll, kwargs, message
):
    """Nothing is added when the slot or the poll type is wrong."""
    stub_client.polls.get_poll_details.return_value = {"poll": poll}
    stub_client.polls.add_option = mocker.AsyncMock()

    with pytest.raises(MCPError, match=message):
        await _call(
            polls_tools, mocker, "nc_polls_add_date_option", poll_id=7, **kwargs
        )

    stub_client.polls.add_option.assert_not_awaited()


async def test_add_text_options_returns_only_the_new_ones(
    polls_tools, stub_client, mocker
):
    """The route answers with every option; the tool reports what it added."""
    existing = _option(1, 0, 1, text="Berlin")
    stub_client.polls.get_poll_details.return_value = {
        "poll": TEXT_POLL_FIXTURE,
        "options": [existing],
    }
    stub_client.polls.add_text_options = mocker.AsyncMock(
        return_value=[
            existing,
            _option(2, 0, 2, text="Hamburg"),
            _option(3, 0, 3, text="Köln"),
        ]
    )

    result = await _call(
        polls_tools,
        mocker,
        "nc_polls_add_text_options",
        poll_id=8,
        texts=[" Hamburg ", "Köln"],
    )

    stub_client.polls.add_text_options.assert_awaited_once_with(8, ["Hamburg", "Köln"])
    assert [o.text for o in result.added] == ["Hamburg", "Köln"]
    assert result.added[0].start is None


@pytest.mark.parametrize("texts", [[], [""], ["a\nb"]])
async def test_add_text_options_rejects_bad_texts(
    polls_tools, stub_client, mocker, texts
):
    """Empty entries or line breaks would split or blank options."""
    stub_client.polls.add_text_options = mocker.AsyncMock()

    with pytest.raises(MCPError, match="line break"):
        await _call(
            polls_tools, mocker, "nc_polls_add_text_options", poll_id=8, texts=texts
        )

    stub_client.polls.add_text_options.assert_not_awaited()


async def test_vote(polls_tools, stub_client, mocker):
    """A vote on an option of the poll is passed through."""
    stub_client.polls.vote = mocker.AsyncMock(
        return_value=_vote(2, "maybe", "alice", "Alice")
    )

    result = await _call(
        polls_tools, mocker, "nc_polls_vote", poll_id=7, option_id=2, answer="maybe"
    )

    stub_client.polls.vote.assert_awaited_once_with(2, "maybe")
    assert (result.poll_id, result.option_id, result.answer) == (7, 2, "maybe")


@pytest.mark.parametrize(
    ("option_id", "answer", "poll", "message"),
    [
        (99, "yes", POLL_FIXTURE, "not an option of poll 7"),
        (3, "yes", POLL_FIXTURE, "not an option of poll 7"),  # deleted option
        (2, "maybe", {**POLL_FIXTURE, "configuration": {"allowMaybe": False}}, "maybe"),
    ],
)
async def test_vote_refuses_what_polls_would_reject(
    polls_tools, stub_client, mocker, option_id, answer, poll, message
):
    """Wrong option or a disallowed 'maybe' never reaches the vote route."""
    stub_client.polls.get_poll_details.return_value = {"poll": poll}
    stub_client.polls.vote = mocker.AsyncMock()

    with pytest.raises(MCPError, match=message):
        await _call(
            polls_tools,
            mocker,
            "nc_polls_vote",
            poll_id=7,
            option_id=option_id,
            answer=answer,
        )

    stub_client.polls.vote.assert_not_awaited()


async def test_add_comment(polls_tools, stub_client, mocker):
    """A comment is posted and returned."""
    stub_client.polls.add_comment = mocker.AsyncMock(
        return_value=_comment(4, "Passt mir", 1790000500)
    )

    result = await _call(
        polls_tools, mocker, "nc_polls_add_comment", poll_id=7, comment="Passt mir"
    )

    stub_client.polls.add_comment.assert_awaited_once_with(7, "Passt mir", False)
    assert result.comment.comment == "Passt mir"


@pytest.mark.parametrize(("comment", "message"), [("  ", "empty"), ("x" * 4001, "max")])
async def test_add_comment_rejects_empty_or_overlong(
    polls_tools, stub_client, mocker, comment, message
):
    """Blank or runaway comments are refused before posting."""
    stub_client.polls.add_comment = mocker.AsyncMock()

    with pytest.raises(MCPError, match=message):
        await _call(
            polls_tools, mocker, "nc_polls_add_comment", poll_id=7, comment=comment
        )

    stub_client.polls.add_comment.assert_not_awaited()


@pytest.mark.parametrize(
    ("current", "requested", "toggled"),
    [
        (0, True, True),
        (1790000000, True, False),
        (1790000000, False, True),
        (0, False, False),
    ],
)
async def test_confirm_option_only_toggles_when_needed(
    polls_tools, stub_client, mocker, current, requested, toggled
):
    """The route is a toggle; the tool turns it into a setter."""
    option = {**_option(1, SLOT_SUMMER, 1), "confirmed": current}
    stub_client.polls.get_poll_details.return_value = {
        "poll": POLL_FIXTURE,
        "options": [option],
    }
    flipped = {**option, "confirmed": 0 if current else 1790000600}
    stub_client.polls.toggle_option_confirmation = mocker.AsyncMock(
        return_value=flipped
    )

    result = await _call(
        polls_tools,
        mocker,
        "nc_polls_confirm_option",
        poll_id=7,
        option_id=1,
        confirmed=requested,
    )

    assert result.changed is toggled
    assert result.option.confirmed is requested
    assert stub_client.polls.toggle_option_confirmation.await_count == int(toggled)


@pytest.mark.parametrize(
    ("tool", "method"),
    [("nc_polls_close_poll", "close_poll"), ("nc_polls_reopen_poll", "reopen_poll")],
)
async def test_close_and_reopen(polls_tools, stub_client, mocker, tool, method):
    """Closing and reopening return the poll in its new state."""
    setattr(stub_client.polls, method, mocker.AsyncMock(return_value=POLL_FIXTURE))

    result = await _call(polls_tools, mocker, tool, poll_id=7)

    getattr(stub_client.polls, method).assert_awaited_once_with(7)
    assert result.poll.id == 7


# --- Errors, links, annotations ---


def _http_error(status, message=None):
    request = httpx.Request("GET", "https://nc.example/ocs/v2.php/apps/polls")
    json = None
    if message is not None:
        json = {
            "ocs": {
                "meta": {"status": "failure", "statuscode": status, "message": message},
                "data": [],
            }
        }
    return httpx.HTTPStatusError(
        "boom",
        request=request,
        response=httpx.Response(status, request=request, json=json),
    )


SQL_MESSAGE = (
    'Did expect one result but found none when executing: query "SELECT '
    "`polls_polls`.* FROM `oc_polls_polls` `polls_polls` WHERE `polls_polls`.`id` "
    '= :dcValue1" ' + "x" * 300
)


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        (404, None, "Failed getting poll 99: not found, or not shared with you"),
        (403, None, "Failed getting poll 99: not allowed"),
        (500, None, "Failed getting poll 99: server error (500)"),
        (
            400,
            "Poll not found",
            "Failed getting poll 99: not found, or not shared with you (Poll not found)",
        ),
        (
            400,
            "denied permission vote",
            "Failed getting poll 99: not allowed — ",
        ),
        (404, SQL_MESSAGE, "Failed getting poll 99: not found, or not shared with you"),
        (
            400,
            "Title must not be empty",
            "Failed getting poll 99: request rejected (400) (Title must not be empty)",
        ),
        (
            500,
            "Internal Server Error",
            "Failed getting poll 99: server error (500) (Internal Server Error)",
        ),
    ],
)
async def test_http_errors_become_mcp_errors(
    polls_tools, stub_client, mocker, status, message, expected
):
    """Each failure is classified by status and message, without SQL text."""
    stub_client.polls.get_poll.side_effect = _http_error(status, message)

    with pytest.raises(MCPError) as excinfo:
        await _call(polls_tools, mocker, "nc_polls_get_poll", poll_id=99)

    text = str(excinfo.value)
    assert text.startswith(expected)
    assert "SELECT" not in text
    if status == 404 and message:
        assert text == expected


async def test_long_server_message_is_cut(polls_tools, stub_client, mocker):
    """A server message reaches the model with at most 200 characters."""
    stub_client.polls.get_poll.side_effect = _http_error(400, "A" * 500)

    with pytest.raises(MCPError) as excinfo:
        await _call(polls_tools, mocker, "nc_polls_get_poll", poll_id=99)

    reason = str(excinfo.value).split("(400) (", 1)[1][:-1]
    assert len(reason) == 200
    assert reason.endswith("…")


async def test_server_reason_is_passed_on(polls_tools, stub_client, mocker):
    """When Polls says why it refused, the model sees that reason."""
    stub_client.polls.close_poll = mocker.AsyncMock(
        side_effect=_http_error(403, "Poll is closed")
    )

    with pytest.raises(MCPError, match="Poll is closed"):
        await _call(polls_tools, mocker, "nc_polls_close_poll", poll_id=7)


@pytest.mark.parametrize(
    ("tool", "kwargs", "url_of"),
    [
        ("nc_polls_list_polls", {}, lambda r: r.results[0].url),
        ("nc_polls_get_poll", {"poll_id": 7}, lambda r: r.poll.url),
        ("nc_polls_get_options", {"poll_id": 7}, lambda r: r.url),
        ("nc_polls_get_results", {"poll_id": 7}, lambda r: r.url),
    ],
)
async def test_responses_link_to_the_poll(
    polls_tools, stub_client, mocker, tool, kwargs, url_of
):
    """Every response carries a link that opens the poll in the browser."""
    mocker.patch(
        "nextcloud_mcp_server.astrolabe_links.get_settings",
        return_value=SimpleNamespace(nextcloud_browser_url="https://nc.example.com"),
    )

    result = await _call(polls_tools, mocker, tool, **kwargs)

    assert url_of(result) == "https://nc.example.com/index.php/apps/polls/vote/7"


async def test_vote_response_links_to_the_poll(polls_tools, stub_client, mocker):
    """Write responses are linked too, from the poll id they carry."""
    mocker.patch(
        "nextcloud_mcp_server.astrolabe_links.get_settings",
        return_value=SimpleNamespace(nextcloud_browser_url="https://nc.example.com"),
    )
    stub_client.polls.vote = mocker.AsyncMock(return_value=None)

    result = await _call(
        polls_tools, mocker, "nc_polls_vote", poll_id=7, option_id=1, answer="yes"
    )

    assert result.url == "https://nc.example.com/index.php/apps/polls/vote/7"
    assert result.answer == "yes"


READ_TOOLS = {
    "nc_polls_list_polls",
    "nc_polls_get_poll",
    "nc_polls_get_options",
    "nc_polls_get_results",
    "nc_polls_get_comments",
}
WRITE_TOOLS = {
    "nc_polls_create_poll": False,
    "nc_polls_update_poll": True,
    "nc_polls_add_date_option": False,
    "nc_polls_add_text_options": False,
    "nc_polls_vote": True,
    "nc_polls_add_comment": False,
    "nc_polls_confirm_option": True,
    "nc_polls_close_poll": True,
    "nc_polls_reopen_poll": True,
}


def test_tool_set_and_annotations(polls_tools):
    """Read tools are read-only; write tools are non-destructive, with the
    idempotency ADR-017 asks for. Nothing deletes."""
    tools = {t.name: t for t in polls_tools.list_tools()}

    assert set(tools) == READ_TOOLS | set(WRITE_TOOLS)
    for name, tool in tools.items():
        assert tool.title
        assert tool.annotations.open_world_hint is True
        assert tool.annotations.destructive_hint is not True
        if name in READ_TOOLS:
            assert tool.annotations.read_only_hint is True
        else:
            assert tool.annotations.read_only_hint is not True
            assert tool.annotations.idempotent_hint is WRITE_TOOLS[name], name
