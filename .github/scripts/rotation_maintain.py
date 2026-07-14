#!/usr/bin/env python3
"""Maintain the Discord-watch schedule: prune elapsed dates, extend the horizon.

Keeps rotation_schedule.json a rolling window of upcoming weekdays. On each run
it drops rows before today and appends new weekday rows — continuing the
rotation order from wherever the schedule currently ends — until the schedule
reaches HORIZON_DAYS ahead. Idempotent: running it twice in a row is a no-op
once the horizon is full, and a missed run just gets caught up on the next one.

Manual edits (swaps, holiday coverage) on future dates are preserved — pruning
only removes past dates, and extension only appends beyond the current last
date, so it never rewrites a row a human changed.

Run with --check to exit non-zero when the file would change (no write), for a
dry run in CI. Otherwise it rewrites the file in place.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib

ROSTER_PATH = pathlib.Path(__file__).with_name("rotation_roster.json")
SCHEDULE_PATH = pathlib.Path(__file__).with_name("rotation_schedule.json")

# Keep the schedule filled this many days into the future.
HORIZON_DAYS = 90


def _roster_order(roster_path: pathlib.Path) -> list[str]:
    """Rotation order = the order names appear in the roster JSON."""
    roster = json.loads(roster_path.read_text())
    return list(roster["people"].keys())


def _next_weekday(date: datetime.date) -> datetime.date:
    """The next Mon–Fri strictly after date."""
    nxt = date + datetime.timedelta(days=1)
    while nxt.weekday() >= 5:  # 5=Sat, 6=Sun
        nxt += datetime.timedelta(days=1)
    return nxt


def maintain(
    schedule_doc: dict,
    order: list[str],
    today: datetime.date,
    horizon_days: int = HORIZON_DAYS,
) -> dict:
    """Return a new schedule doc with past dates pruned and horizon extended."""
    rows = schedule_doc.get("schedule", [])

    # Prune elapsed dates (keep today onward).
    kept = [r for r in rows if datetime.date.fromisoformat(r["date"]) >= today]
    kept.sort(key=lambda r: r["date"])

    # Figure out where to resume the rotation.
    if kept:
        last_date = datetime.date.fromisoformat(kept[-1]["date"])
        last_idx = order.index(kept[-1]["name"]) if kept[-1]["name"] in order else -1
    else:
        # Empty (or fully elapsed) schedule: start today, at the top of the order.
        last_date = today - datetime.timedelta(days=1)
        last_idx = -1

    horizon = today + datetime.timedelta(days=horizon_days)
    date = _next_weekday(last_date) if kept else _first_weekday_on_or_after(today)
    idx = last_idx
    while date <= horizon:
        idx = (idx + 1) % len(order)
        kept.append({"date": date.isoformat(), "name": order[idx]})
        date = _next_weekday(date)

    new_doc = dict(schedule_doc)
    new_doc["schedule"] = kept
    return new_doc


def _first_weekday_on_or_after(date: datetime.date) -> datetime.date:
    while date.weekday() >= 5:
        date += datetime.timedelta(days=1)
    return date


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the file would change; do not write",
    )
    parser.add_argument(
        "--today",
        type=datetime.date.fromisoformat,
        default=datetime.date.today(),
        help="override today's date (ISO), for testing",
    )
    args = parser.parse_args()

    doc = json.loads(SCHEDULE_PATH.read_text())
    order = _roster_order(ROSTER_PATH)
    new_doc = maintain(doc, order, args.today)

    old_text = SCHEDULE_PATH.read_text()
    new_text = json.dumps(new_doc, indent=2) + "\n"

    if old_text == new_text:
        print("Schedule already current; no change.")
        return 0

    old_n = len(doc.get("schedule", []))
    new_n = len(new_doc["schedule"])
    print(
        f"Schedule updated: {old_n} -> {new_n} rows (through {new_doc['schedule'][-1]['date']})."
    )

    if args.check:
        print("(--check) not writing.")
        return 1

    SCHEDULE_PATH.write_text(new_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
