#!/usr/bin/env python3
"""Daily Discord-watch rotation reminder.

Reads an explicit dated schedule (rotation_schedule.json) plus a name ->
slack_id/timezone roster (rotation_roster.json), finds today's assignee, and
pings them in Slack on the morning of *their* local timezone.

The GitHub Actions workflow wakes at a couple of fixed UTC times (one per
timezone's morning). On each run the day's assignee is pinged only if it's
currently morning where they live; if not, the run for their timezone's
morning handles them. Our timezones are far enough apart that only one is ever
in its morning at a time, so at most one person is pinged per run. Dates not
present in the schedule get no ping.

Set SLACK_WEBHOOK_URL to post for real. Leave it unset for a dry run that just
prints what it would do — handy for testing the schedule without Slack.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import urllib.error
import urllib.request
from dataclasses import dataclass
from zoneinfo import ZoneInfo

# Data files live alongside this script so they can be edited (swaps,
# holidays, extending the schedule) without touching the logic here.
ROSTER_PATH = pathlib.Path(__file__).with_name("rotation_roster.json")
SCHEDULE_PATH = pathlib.Path(__file__).with_name("rotation_schedule.json")

# Each cron run is one timezone's morning scan: we ping today's assignee only
# if it's currently morning where they are. A run that's morning in SF is night
# in Singapore and vice versa, so at most one timezone matches per run. Morning
# is a band rather than an exact hour, which absorbs both daylight saving and
# GitHub's frequently-delayed cron schedule — a run that fires a few hours late
# still counts as that person's morning. The band starts at 05:00 (not
# midnight) so a delayed *other* timezone's cron spilling past local midnight
# isn't mistaken for this timezone's morning, which would double-ping.
MORNING_START_HOUR = 5
MORNING_END_HOUR = 12


@dataclass(frozen=True)
class Person:
    name: str  # display name; matches the names used in the schedule
    slack_id: str  # Slack member ID, e.g. "U01ABC2DEF" (NOT the display name)
    tz: str  # IANA timezone name, e.g. "America/Los_Angeles"


def load_roster(roster_path: pathlib.Path = ROSTER_PATH) -> dict[str, Person]:
    """Load the name -> Person mapping from JSON."""
    roster = json.loads(roster_path.read_text())
    return {
        name: Person(name=name, slack_id=entry["slack_id"], tz=entry["tz"])
        for name, entry in roster["people"].items()
    }


def load_schedule(
    schedule_path: pathlib.Path = SCHEDULE_PATH,
) -> dict[datetime.date, str]:
    """Load the date -> assignee-name mapping from JSON."""
    doc = json.loads(schedule_path.read_text())
    return {datetime.date.fromisoformat(row["date"]): row["name"] for row in doc["schedule"]}


ROSTER: dict[str, Person] = load_roster()
SCHEDULE: dict[datetime.date, str] = load_schedule()


def assignee_for(local_date: datetime.date) -> Person | None:
    """The person scheduled for a given date, or None if the date isn't listed."""
    name = SCHEDULE.get(local_date)
    if name is None:
        return None
    return ROSTER.get(name)


def whose_turn_now(now_utc: datetime.datetime) -> Person | None:
    """Return the person to ping right now, or None if it isn't anyone's morning.

    Each person is evaluated in their own timezone: it must currently be morning
    (05:00–11:59) there, and today's schedule entry must name them. Since our
    timezones are far enough apart that only one is ever in its morning at a
    time, at most one person matches. A person missed by a late/early run is
    picked up by the next run that lands in their morning.
    """
    for person in ROSTER.values():
        local = now_utc.astimezone(ZoneInfo(person.tz))
        if not (MORNING_START_HOUR <= local.hour < MORNING_END_HOUR):
            continue
        if assignee_for(local.date()) == person:
            return person
    return None


class SlackPostError(RuntimeError):
    """Raised when the Slack POST fails, without exposing the webhook URL."""


def post_to_slack(webhook_url: str, person: Person) -> None:
    text = (
        f"<@{person.slack_id}> you're on *Discord watch* today \U0001f440 "
        f"— please keep an eye on the channel."
    )
    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    # Catch and re-raise without the URL: urllib errors stringify the full
    # webhook URL, which must never reach the Actions log or error output.
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise SlackPostError(f"Slack returned HTTP {exc.code} {exc.reason}") from None
    except urllib.error.URLError as exc:
        raise SlackPostError(f"could not reach Slack: {exc.reason}") from None


def _report_todays_assignees(now_utc: datetime.datetime) -> None:
    """Log who's on watch for each timezone's current local date.

    Runs regardless of the morning window so a manual run is always
    informative, even outside anyone's ping window.
    """
    for tz in sorted({p.tz for p in ROSTER.values()}):
        local = now_utc.astimezone(ZoneInfo(tz))
        person = assignee_for(local.date())
        who = person.name if person else "nobody (no schedule entry)"
        print(f"  {tz}: {local:%Y-%m-%d %a} -> {who}")


def main() -> None:
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    print(f"Today's watch by timezone (as of {now_utc:%Y-%m-%d %H:%M UTC}):")
    _report_todays_assignees(now_utc)

    person = whose_turn_now(now_utc)

    if person is None:
        print(f"{now_utc:%Y-%m-%d %H:%M UTC}: nobody's on watch right now, nothing to do.")
        return

    local = now_utc.astimezone(ZoneInfo(person.tz))
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print(
            f"[dry run] Would ping {person.name} ({person.slack_id}) "
            f"— it's {local:%Y-%m-%d %H:%M} in {person.tz}. "
            f"Set SLACK_WEBHOOK_URL to post for real."
        )
        return

    post_to_slack(webhook_url, person)
    print(f"Pinged {person.name} ({person.slack_id}) at {local:%Y-%m-%d %H:%M %Z}.")


if __name__ == "__main__":
    main()
