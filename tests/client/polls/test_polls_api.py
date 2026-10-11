"""Mocked unit tests for the Polls client and its models.

What these pin down is the behaviour the rest of the codebase cannot see: that
the OCS envelope is unwrapped, that ``OCS-APIRequest`` goes out on every call
(without it Nextcloud answers 997, not a 4xx), that Polls' "0 means unset"
timestamps become ``None``, that date options are rendered in the poll's own
timezone, and that the tally sorts by approval.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from nextcloud_mcp_server.client.polls import PollsClient, iso_duration
from nextcloud_mcp_server.models.polls import (
    Poll,
    PollOption,
    PollSummary,
    poll_timezone,
    tally_results,
)
from tests.client.conftest import create_mock_response

pytestmark = pytest.mark.unit

API = "/ocs/v2.php/apps/polls/api/v1.0"
BERLIN = ZoneInfo("Europe/Berlin")

# 2026-10-20 16:00 UTC (18:00 CEST) and 2026-10-27 17:00 UTC (18:00 CET): the
# second slot is after the DST switch, so a fixed offset would get one wrong.
SLOT_SUMMER = 1792512000
SLOT_WINTER = 1793120400


def _ocs(data):
    """Wrap *data* in the envelope the app's OCS routes answer with."""
    return {
        "ocs": {
            "meta": {"status": "ok", "statuscode": 200, "message": "OK"},
            "data": data,
        }
    }


def _client(mocker, json_data):
    """A client whose ``_make_request`` is patched, plus the patch itself."""
    mock_make_request = mocker.patch.object(
        PollsClient,
        "_make_request",
        return_value=create_mock_response(status_code=200, json_data=json_data),
    )
    client = PollsClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")
    return client, mock_make_request


def _user(user_id, display_name):
    return {"id": user_id, "userId": user_id, "displayName": display_name}


POLL_FIXTURE = {
    "id": 7,
    "type": "datePoll",
    "votingVariant": "simple",
    "descriptionSafe": "",
    "configuration": {
        "title": "Vorstandssitzung",
        "description": "Bitte bis Freitag abstimmen",
        "access": "private",
        "allowComment": True,
        "allowMaybe": True,
        "allowProposals": "disallow",
        "anonymous": False,
        "expire": SLOT_SUMMER - 86400,
        "showResults": "always",
        "timezoneName": "Europe/Berlin",
        "useNo": True,
        "maxVotesPerOption": 0,
        "maxVotesPerUser": 0,
    },
    "owner": _user("alice", "Alice A."),
    "status": {
        "created": 1790000000,
        "lastInteraction": 1791000000,
        "isAnonymous": False,
        "isArchived": False,
        "isExpired": False,
        "countParticipants": 3,
    },
}

TEXT_POLL_FIXTURE = {
    "id": 8,
    "type": "textPoll",
    "votingVariant": "simple",
    "configuration": {"title": "Ort", "expire": 0, "timezoneName": None},
    "owner": _user("bob", "Bob B."),
    "status": {"isArchived": True, "isExpired": True},
}


def _option(option_id, timestamp, order, duration=7200, deleted=0, text=""):
    return {
        "id": option_id,
        "pollId": 7,
        "text": text or str(timestamp),
        "timestamp": timestamp,
        "duration": duration,
        "deleted": deleted,
        "order": order,
        "confirmed": 0,
        "locked": False,
        "votes": {"yes": 0, "no": 0, "maybe": 0, "count": 0, "currentUser": None},
    }


def _vote(option_id, answer, user_id, display_name, deleted=0):
    return {
        "id": option_id * 1000 + len(user_id),
        "pollId": 7,
        "optionId": option_id,
        "optionText": "",
        "answer": answer,
        "deleted": deleted,
        "user": _user(user_id, display_name),
        "answerSymbol": "",
    }


# --- Client ---


async def test_list_polls_unwraps_envelope_and_sends_ocs_headers(mocker):
    client, mock_make_request = _client(
        mocker, _ocs({"polls": [POLL_FIXTURE, TEXT_POLL_FIXTURE]})
    )

    polls = await client.list_polls()

    assert [p["id"] for p in polls] == [7, 8]
    mock_make_request.assert_called_once_with(
        "GET",
        f"{API}/polls",
        headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        params={"format": "json"},
    )


async def test_list_polls_empty(mocker):
    client, _ = _client(mocker, _ocs({"polls": []}))
    assert await client.list_polls() == []


async def test_get_poll_returns_poll_object(mocker):
    client, mock_make_request = _client(
        mocker,
        _ocs({"poll": POLL_FIXTURE, "options": [], "votes": [], "comments": []}),
    )

    poll = await client.get_poll(7)

    assert poll["id"] == 7
    assert mock_make_request.call_args.args == ("GET", f"{API}/poll/7")


async def test_get_poll_details_returns_whole_payload(mocker):
    payload = {"poll": POLL_FIXTURE, "options": [], "votes": [], "comments": []}
    client, mock_make_request = _client(mocker, _ocs(payload))

    assert await client.get_poll_details(7) == payload
    assert mock_make_request.call_args.args == ("GET", f"{API}/poll/7")


@pytest.mark.parametrize(
    ("method", "path", "key"),
    [
        ("get_options", "/poll/7/options", "options"),
        ("get_votes", "/poll/7/votes", "votes"),
    ],
)
async def test_option_and_vote_routes(mocker, method, path, key):
    client, mock_make_request = _client(mocker, _ocs({key: [{"id": 1}]}))

    assert await getattr(client, method)(7) == [{"id": 1}]
    assert mock_make_request.call_args.args == ("GET", f"{API}{path}")


@pytest.mark.parametrize("bad_id", [0, -1, True, "7"])
async def test_invalid_poll_id_never_reaches_the_wire(mocker, bad_id):
    client, mock_make_request = _client(mocker, _ocs({}))

    with pytest.raises(ValueError, match="Poll ID"):
        await client.get_poll(bad_id)

    mock_make_request.assert_not_called()


async def test_http_error_propagates(mocker):
    request = httpx.Request("GET", f"https://nc.example{API}/poll/99")
    response = httpx.Response(404, request=request)
    mocker.patch.object(
        PollsClient,
        "_make_request",
        side_effect=httpx.HTTPStatusError(
            "not found", request=request, response=response
        ),
    )
    client = PollsClient(mocker.AsyncMock(spec=httpx.AsyncClient), "testuser")

    with pytest.raises(httpx.HTTPStatusError):
        await client.get_poll(99)


# --- Models ---


def test_poll_summary_maps_list_fields():
    summary = PollSummary.from_api(POLL_FIXTURE)

    assert summary.id == 7
    assert summary.title == "Vorstandssitzung"
    assert summary.type == "datePoll"
    assert summary.owner.user_id == "alice"
    assert summary.owner.display_name == "Alice A."
    assert summary.expire == datetime(2026, 10, 19, 18, 0, tzinfo=BERLIN)
    assert summary.is_archived is False


def test_poll_summary_expire_zero_means_no_deadline():
    summary = PollSummary.from_api(TEXT_POLL_FIXTURE)

    assert summary.expire is None
    assert summary.is_archived is True
    assert summary.is_expired is True


def test_poll_maps_configuration_and_status():
    poll = Poll.from_api(POLL_FIXTURE)

    assert poll.configuration.allow_maybe is True
    assert poll.configuration.timezone_name == "Europe/Berlin"
    assert poll.configuration.description == "Bitte bis Freitag abstimmen"
    assert poll.status.created == datetime.fromtimestamp(1790000000, tz=timezone.utc)
    assert poll.status.count_participants == 3


@pytest.mark.parametrize("name", [None, "", "Not/AZone"])
def test_unknown_timezone_falls_back_to_utc(name):
    assert poll_timezone(name) == timezone.utc


def test_date_option_is_rendered_in_poll_timezone_across_dst():
    summer = PollOption.from_api(_option(1, SLOT_SUMMER, 1), "datePoll", BERLIN)
    winter = PollOption.from_api(_option(2, SLOT_WINTER, 2), "datePoll", BERLIN)

    # Both slots are 18:00 local time even though the UTC offset differs.
    assert summer.start == datetime(2026, 10, 20, 18, 0, tzinfo=BERLIN)
    assert summer.start.utcoffset() == timedelta(hours=2)
    assert winter.start == datetime(2026, 10, 27, 18, 0, tzinfo=BERLIN)
    assert winter.start.utcoffset() == timedelta(hours=1)
    assert summer.end - summer.start == timedelta(hours=2)
    assert summer.start.isoformat() == "2026-10-20T18:00:00+02:00"


def test_zero_duration_option_ends_when_it_starts():
    option = PollOption.from_api(
        _option(1, SLOT_SUMMER, 1, duration=0), "datePoll", BERLIN
    )
    assert option.end == option.start


def test_text_option_has_no_times():
    option = PollOption.from_api(
        _option(1, 0, 1, text="Berlin"), "textPoll", timezone.utc
    )
    assert option.text == "Berlin"
    assert option.start is None and option.end is None


def test_tally_counts_names_and_sorts_by_approval():
    options = [
        _option(1, SLOT_SUMMER, 1),
        _option(2, SLOT_WINTER, 2),
        _option(3, SLOT_WINTER + 86400, 3),
        _option(4, SLOT_WINTER + 2 * 86400, 4, deleted=1790000000),
    ]
    votes = [
        _vote(1, "yes", "alice", "Alice"),
        _vote(1, "no", "bob", "Bob"),
        _vote(2, "yes", "alice", "Alice"),
        _vote(2, "yes", "bob", "Bob"),
        _vote(2, "maybe", "carol", "Carol"),
        _vote(3, "yes", "carol", "Carol"),
        _vote(3, "yes", "dave", "Dave"),
        _vote(3, "yes", "erin", "Erin", deleted=1790000000),
        _vote(4, "yes", "alice", "Alice"),
    ]

    results, participants = tally_results(options, votes, "datePoll", BERLIN)

    # Option 4 is deleted; erin's only vote is deleted.
    assert [r.option_id for r in results] == [2, 3, 1]
    best = results[0]
    assert (best.yes, best.maybe, best.no) == (2, 1, 0)
    assert best.yes_voters == ["Alice", "Bob"]
    assert best.maybe_voters == ["Carol"]
    assert best.start == datetime(2026, 10, 27, 18, 0, tzinfo=BERLIN)
    assert results[2].no_voters == ["Bob"]
    assert participants == ["Alice", "Bob", "Carol", "Dave"]


def test_tally_keeps_options_without_votes():
    results, participants = tally_results(
        [_option(1, SLOT_SUMMER, 1)], [], "datePoll", BERLIN
    )

    assert len(results) == 1
    assert (results[0].yes, results[0].maybe, results[0].no) == (0, 0, 0)
    assert participants == []


# --- Writes ---


@pytest.mark.parametrize(
    ("call", "method", "path", "body", "key"),
    [
        (
            lambda c: c.create_poll("T", "datePoll"),
            "POST",
            "/poll",
            {"title": "T", "type": "datePoll", "votingVariant": "simple"},
            "poll",
        ),
        (
            lambda c: c.update_poll(7, {"title": "T"}),
            "PUT",
            "/poll/7",
            {"pollConfiguration": {"title": "T"}},
            "poll",
        ),
        (
            lambda c: c.add_option(7, "2026-10-23T18:00:00+02:00", "PT2H"),
            "POST",
            "/poll/7/option",
            {
                "option": {
                    "isoTimestamp": "2026-10-23T18:00:00+02:00",
                    "isoDuration": "PT2H",
                }
            },
            "option",
        ),
        (
            lambda c: c.add_option(7, "2026-10-23T18:00:00+02:00", None),
            "POST",
            "/poll/7/option",
            {"option": {"isoTimestamp": "2026-10-23T18:00:00+02:00"}},
            "option",
        ),
        (
            lambda c: c.add_text_options(7, ["A", "B"]),
            "POST",
            "/poll/7/options",
            {"text": "A\nB"},
            "options",
        ),
        (
            lambda c: c.add_comment(7, "Hallo"),
            "POST",
            "/poll/7/comment",
            {"comment": "Hallo", "confidential": False},
            "comment",
        ),
    ],
)
async def test_write_routes_send_the_documented_body(
    mocker, call, method, path, body, key
):
    """Each write hits its route with the body openapi.json documents."""
    client, mock_make_request = _client(mocker, _ocs({key: {"id": 1}}))

    await call(client)

    assert mock_make_request.call_args.args == (method, f"{API}{path}")
    assert mock_make_request.call_args.kwargs["json"] == body


@pytest.mark.parametrize(
    ("call", "path", "key"),
    [
        (lambda c: c.vote(5, "yes"), "/option/5/vote/yes", "vote"),
        (lambda c: c.toggle_option_confirmation(5), "/option/5/confirm", "option"),
        (lambda c: c.close_poll(7), "/poll/7/close", "poll"),
        (lambda c: c.reopen_poll(7), "/poll/7/reopen", "poll"),
    ],
)
async def test_bodyless_puts_send_no_json(mocker, call, path, key):
    """Routes without a body get no ``json=`` and so no Content-Type."""
    client, mock_make_request = _client(mocker, _ocs({key: {"id": 1}}))

    await call(client)

    assert mock_make_request.call_args.args == ("PUT", f"{API}{path}")
    assert "json" not in mock_make_request.call_args.kwargs


async def test_get_comments(mocker):
    client, mock_make_request = _client(mocker, _ocs({"comments": [{"id": 1}]}))

    assert await client.get_comments(7) == [{"id": 1}]
    assert mock_make_request.call_args.args == ("GET", f"{API}/poll/7/comments")


@pytest.mark.parametrize("answer", ["", "YES", "../poll/1", "abstain"])
async def test_vote_answer_is_checked_before_it_reaches_the_path(mocker, answer):
    """The answer is a URL path segment, so only Polls' three values pass."""
    client, mock_make_request = _client(mocker, _ocs({}))

    with pytest.raises(ValueError, match="Answer must be one of"):
        await client.vote(5, answer)

    mock_make_request.assert_not_called()


@pytest.mark.parametrize("bad_id", [0, -3, True])
async def test_invalid_option_id_never_reaches_the_wire(mocker, bad_id):
    client, mock_make_request = _client(mocker, _ocs({}))

    with pytest.raises(ValueError, match="Option ID"):
        await client.vote(bad_id, "yes")

    mock_make_request.assert_not_called()


@pytest.mark.parametrize(
    ("seconds", "use_days", "expected"),
    [
        (0, True, None),
        (60, True, "PT1M"),
        (5400, True, "PT1H30M"),
        (7200, True, "PT2H"),
        (5401, True, "PT1H30M1S"),
        (86400, True, "P1D"),
        (93600, True, "P1DT2H"),
        (2 * 86400, True, "P2D"),
        (86400, False, "PT24H"),
        (49 * 3600, False, "PT49H"),
        (93600, False, "PT26H"),
    ],
)
def test_iso_duration(seconds, use_days, expected):
    """Only canonical ISO 8601 durations go out; Polls answers others with 500."""
    assert iso_duration(seconds, use_days=use_days) == expected


def test_iso_duration_rejects_negative():
    with pytest.raises(ValueError, match="negative"):
        iso_duration(-1)
