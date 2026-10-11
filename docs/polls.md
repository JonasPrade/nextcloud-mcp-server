# Polls App

Read-only access to the [Nextcloud Polls](https://github.com/nextcloud/polls) app: date polls (scheduling) and text polls. Writing (creating polls, voting) is not supported yet.

Enable with `--enable-app polls`. The tools need the `polls.read` scope and are hidden when the instance does not advertise the `polls` capability, i.e. when the app is not installed or not enabled for the user.

### Polls Tools

| Tool | Description |
|------|-------------|
| `nc_polls_list_polls` | List every poll the user owns or can access: ID, title, type, deadline, archived/expired flags, owner |
| `nc_polls_get_poll` | Get a single poll with its configuration (description, access, `allow_maybe`, `show_results`, timezone, …) and status |
| `nc_polls_get_options` | Get the options of a poll in poll order; date options carry `start` and `end` in the poll's timezone |
| `nc_polls_get_results` | Get the tally: per option the yes/maybe/no counts and voter names, sorted by most "yes", then most "maybe" |

## Times and timezones

Polls stores times as Unix timestamps. The tools return them as ISO 8601 datetimes with an explicit offset, in the timezone the poll was created in (`configuration.timezoneName`), falling back to UTC when the poll has none. A date option is stored as a start timestamp plus a duration in seconds; `end` is `start + duration`, so a zero-duration option has `end == start`. A deadline (`expire`) of `0` in Polls means "no deadline" and is returned as `null`.

## Results

`nc_polls_get_results` counts from the vote list, not from the per-option counters, so every count matches the names listed next to it. Deleted options and votes are skipped. Only votes the user may see are included: on a poll whose results are hidden from the user (`show_results` set to `closed` or `never`), the tally contains only the user's own votes.

## API

The client calls the Polls OCS API under `/ocs/v2.php/apps/polls/api/v1.0` (`GET /polls`, `/poll/{id}`, `/poll/{id}/options`, `/poll/{id}/votes`). Field names follow the app's [`openapi.json`](https://github.com/nextcloud/polls/blob/main/openapi.json).
