# Polls App

Access to the [Nextcloud Polls](https://github.com/nextcloud/polls) app: date polls (scheduling) and text polls. Read polls, options, results and comments; create and configure polls, add options, vote, comment, confirm an option, and close or reopen a poll.

Enable with `--enable-app polls`. Reading needs the `polls.read` scope, writing `polls.write`. The tools are hidden when the instance does not advertise the `polls` capability, i.e. when the app is not installed or not enabled for the user.

### Read tools (`polls.read`)

| Tool | Description |
|------|-------------|
| `nc_polls_list_polls` | List every poll the user owns or can access: ID, title, type, deadline, archived/expired flags, owner |
| `nc_polls_get_poll` | Get a single poll with its configuration (description, access, `allow_maybe`, `show_results`, timezone, …) and status |
| `nc_polls_get_options` | Get the options of a poll in poll order; date options carry `start` and `end` in the poll's timezone |
| `nc_polls_get_results` | Get the tally: per option the yes/maybe/no counts and voter names, sorted by most "yes", then most "maybe" |
| `nc_polls_get_comments` | Get the comments on a poll, oldest first |

### Write tools (`polls.write`)

| Tool | Description |
|------|-------------|
| `nc_polls_create_poll` | Create a date or text poll, optionally with description, deadline, "maybe" answers, access (`private`/`open`) and timezone (default: the user's own timezone) |
| `nc_polls_update_poll` | Change only the given settings: title, description, deadline (or remove it), "maybe" answers, access |
| `nc_polls_add_date_option` | Add a date/time slot to a date poll, with `start` and either `end` or `duration_minutes`, or an all-day slot (`all_day`) |
| `nc_polls_add_text_options` | Add one or more text answers to a text poll in one call |
| `nc_polls_vote` | Answer `yes`, `maybe` or `no` on an option; voting again replaces the earlier answer |
| `nc_polls_add_comment` | Comment on a poll, optionally confidential (visible only to the owner and the author) |
| `nc_polls_confirm_option` | Mark an option as the confirmed outcome, or withdraw that (`confirmed: false`) |
| `nc_polls_close_poll` / `nc_polls_reopen_poll` | Close a poll for voting, or reopen it (reopening removes the deadline) |

Deleting and archiving polls, options or comments, and sharing a poll (which notifies the invited people) are deliberately not exposed. A newly created `private` poll is therefore only visible to its owner until it is shared in the Polls app; an `open` poll is visible to every user of the instance.

## Links

Every response carries a `url` that opens the poll in the browser (`/index.php/apps/polls/vote/{id}`), for list entries per poll. Offer it to the user so they can view the poll, vote or share it. It is `null` when the server has no browser-reachable Nextcloud base URL configured (see ADR-035).

## Times and timezones

Polls stores times as Unix timestamps. The tools return them as ISO 8601 datetimes with an explicit offset, in the timezone the poll was created in (`configuration.timezoneName`), falling back to UTC when the poll has none. A date option is stored as a start timestamp plus a duration in seconds; `end` is `start + duration`, so a zero-duration option has `end == start`. A deadline (`expire`) of `0` in Polls means "no deadline" and is returned as `null`.

Times passed to the write tools (`start`, `end`, `expire`) are ISO 8601. One with an offset (`2026-10-20T18:00+02:00`) is taken as given; one without (`2026-10-20T18:00`) is read in the poll's timezone, or in UTC if the poll has none. The response shows what was stored, with its offset.

`nc_polls_create_poll` always sets the poll's timezone: `timezone_name` if given, else the owner's own timezone as Nextcloud reports it, else UTC. Polls 9.3 stores the voting variant (`simple`) as the timezone of every poll created over the API, so the tool corrects it in the follow-up update.

Date options are sent to Polls as `isoTimestamp` (the start in the poll's timezone, with offset) plus `isoDuration` (e.g. `PT1H30M`), the way the Polls web interface stores them. A point in time has no duration. If the poll already has a slot with the same start and length, Polls returns the existing option; the tool then adds nothing and says so in `note`.

With `all_day: true`, `start` and the optional `end` are dates (`2026-10-30`, `end` inclusive). The slot runs from midnight of `start` to midnight after `end` in the poll's timezone and is sent in hours (`PT49H` across a DST switch), because Polls adds days in the start's fixed offset. Whether the Polls web interface shows such a slot as all-day has not been verified.

## Write behaviour

- `nc_polls_vote` and `nc_polls_confirm_option` first check that the option belongs to the poll; a vote of `maybe` is refused when the poll does not allow it.
- Polls' confirm route is a toggle. `nc_polls_confirm_option` reads the current state first and only toggles when needed, so calling it twice with the same argument leaves the option as asked.
- Polls 9.3 accepts a `maybe` vote even when the poll does not allow it; the tool's check is the only one.
- Closing a poll sets its deadline to the moment of closing; comments stay possible and confirmed options stay confirmed. Reopening removes the deadline entirely, the earlier one is lost.
- Errors are classified by status and message, because Polls reports a missing poll as 400 "Poll not found" on some routes and as 404 with the text of a failed SQL query on others, and a refused action as 400 "denied …". The SQL text is dropped; other messages Polls gives are passed on, cut to 200 characters.

## Results

`nc_polls_get_results` counts from the vote list, not from the per-option counters, so every count matches the names listed next to it. Deleted options and votes are skipped. Only votes the user may see are included: on a poll whose results are hidden from the user (`show_results` set to `closed` or `never`), the tally contains only the user's own votes.

## API

The client calls the Polls OCS API under `/ocs/v2.php/apps/polls/api/v1.0`. Field names and request bodies follow the app's [`openapi.json`](https://github.com/nextcloud/polls/blob/main/openapi.json).
