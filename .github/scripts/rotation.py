#!/usr/bin/env python3
"""Daily Discord-watch rotation reminder.

Picks the person on watch for the current day and pings them in Slack on the
morning of *their* local timezone. The rotation is deterministic — the
assignee is a function of the date and the person's position in the list — so
there is no state to store anywhere.

The GitHub Actions workflow wakes at a couple of fixed UTC times (one per
timezone's morning). On each run the day's assignee is pinged only if it's
currently morning where they live; if not, the run for their timezone's
morning handles them. Our timezones are far enough apart that only one is ever
in its morning at a time, so at most one person is pinged per run.

Set SLACK_WEBHOOK_URL to post for real. Leave it unset for a dry run that just
prints what it would do — handy for testing the rotation order without Slack.
"""

from __future__ import annotations

import datetime
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from zoneinfo import ZoneInfo

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

# Skip Saturdays and Sundays (in each person's local time). The rotation also
# advances by workdays only, so Friday hands off straight to Monday.
WEEKDAYS_ONLY = True

# Rotation anchor: workday 0 is this date. Any Monday works; it only sets the
# phase of the cycle, not who is in it.
EPOCH = datetime.date(2026, 1, 5)  # a Monday


@dataclass(frozen=True)
class Person:
    name: str  # for logs / dry-run output only
    slack_id: str  # Slack member ID, e.g. "U01ABC2DEF" (NOT the display name)
    tz: str  # IANA timezone name, e.g. "America/Los_Angeles"
    # Out-of-office spans as inclusive (start, end) ISO date pairs, e.g.
    # (("2026-07-13", "2026-07-17"),). On any OOO day the person is skipped and
    # the next available person covers; the OOO person keeps their later slots.
    ooo: tuple[tuple[str, str], ...] = ()


# Rotation order. Slack member IDs (profile -> ⋮ More -> Copy member ID) and
# each person's IANA timezone.
PEOPLE: list[Person] = [
    Person("Aravind Segu", "U01A12R8NUR", "America/Los_Angeles"),
    Person("Bryan Qiu", "U05KA5T983Y", "America/Los_Angeles"),
    Person("Daniel Lok", "U060CNWNHSQ", "Asia/Singapore"),
    Person("Dhruv Gupta", "U0A76097E1F", "America/Los_Angeles"),
    Person("Edwin He", "U077B1V6WQJ", "America/Los_Angeles"),
    Person("Pat Sukprasert", "U05HRKWFY81", "Asia/Singapore"),
    Person("Sabhya Chhabria", "U07A1KQDXAB", "America/Los_Angeles"),
    Person("Serena Ruan", "U0571L5KNLR", "Asia/Singapore"),
    Person("Shivam Mittal", "U09FZKX9S6B", "America/Los_Angeles"),
    Person("Tomu Hirata", "U07TX4PR5MZ", "Asia/Singapore"),
    Person("Zeyi (Rice) Fan", "U09L5HT4CH0", "America/Los_Angeles"),
]


def _workdays_between(start: datetime.date, end: datetime.date) -> int:
    """Number of Mon–Fri days in [start, end). Negative if end precedes start."""
    if end < start:
        return -_workdays_between(end, start)
    full_weeks, extra = divmod((end - start).days, 7)
    count = full_weeks * 5
    for i in range(extra):
        if (start + datetime.timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def is_ooo(person: Person, local_date: datetime.date) -> bool:
    """Whether person is out of office on local_date (inclusive spans)."""
    for start, end in person.ooo:
        if datetime.date.fromisoformat(start) <= local_date <= datetime.date.fromisoformat(end):
            return True
    return False


def assignee_for(local_date: datetime.date) -> Person | None:
    """The person on watch for a given local workday, or None if all are OOO.

    Indexed by the number of workdays since EPOCH (which is itself a Monday),
    so weekends advance nobody and Friday hands off directly to Monday. If the
    slot's person is OOO, the next available person covers — probing forward so
    coverage stays a pure function of the date (no stored state). Only
    meaningful for weekdays; weekends are filtered out before this is called.
    """
    workday_number = _workdays_between(EPOCH, local_date)
    for offset in range(len(PEOPLE)):
        person = PEOPLE[(workday_number + offset) % len(PEOPLE)]
        if not is_ooo(person, local_date):
            return person
    return None  # everyone is OOO that day


def whose_turn_now(now_utc: datetime.datetime) -> Person | None:
    """Return the person to ping right now, or None if it isn't anyone's morning.

    Each person is evaluated in their own timezone: it must be a weekday morning
    (before noon) there, and today's rotation slot must land on them. Since our
    timezones are far enough apart that only one is ever in its morning at a
    time, at most one person matches. A person missed by a late/early run is
    picked up by the next run that lands in their morning.
    """
    for person in PEOPLE:
        local = now_utc.astimezone(ZoneInfo(person.tz))
        if not (MORNING_START_HOUR <= local.hour < MORNING_END_HOUR):
            continue
        if WEEKDAYS_ONLY and local.weekday() >= 5:  # 5=Sat, 6=Sun
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
    for tz in sorted({p.tz for p in PEOPLE}):
        local = now_utc.astimezone(ZoneInfo(tz))
        if local.weekday() >= 5:  # 5=Sat, 6=Sun
            who = "nobody (weekend)"
        else:
            person = assignee_for(local.date())
            who = person.name if person else "nobody (all OOO)"
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
