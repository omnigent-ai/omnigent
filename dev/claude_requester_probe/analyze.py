"""Summarize diagnostic mod evidence; never produce an authorization grant."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def summarize(events: list[dict]) -> dict:
    """Join calls only to unique preceding provider emissions in the same incarnation."""
    events = sorted(events, key=lambda event: (event["incarnation"], event["sequence"]))
    emissions: dict[tuple, list[dict]] = defaultdict(list)
    seen_sequences: set[tuple] = set()
    invalid_incarnations: set[str] = set()
    for event in events:
        key = (event["incarnation"], event["sequence"])
        if key in seen_sequences:
            invalid_incarnations.add(event["incarnation"])
        seen_sequences.add(key)
        if event["kind"] == "tool.emitted":
            emissions[
                (event["incarnation"], event.get("session_id"), event.get("tool_use_id"))
            ].append(event)
    calls = []
    for event in events:
        if event["kind"] != "tool.call":
            continue
        candidates = emissions[
            (event["incarnation"], event.get("session_id"), event.get("tool_use_id"))
        ]
        emitted = candidates[0] if len(candidates) == 1 else None
        matched = (
            event["incarnation"] not in invalid_incarnations
            and isinstance(event.get("session_id"), str)
            and bool(event["session_id"])
            and isinstance(event.get("tool_use_id"), str)
            and bool(event["tool_use_id"])
            and emitted is not None
            and isinstance(emitted.get("turn_id"), str)
            and bool(emitted["turn_id"])
            and emitted["sequence"] < event["sequence"]
            and emitted.get("agent_id") == event.get("agent_id")
            and emitted.get("tool") == event.get("tool")
        )
        calls.append(
            {
                "tool_use_id": event.get("tool_use_id"),
                "tool": event.get("tool"),
                "turn_id": emitted["turn_id"] if matched else None,
                "correlation": "observed" if matched else "unresolved",
                "requester": "unverified",
            }
        )
    return {
        "events": len(events),
        "provider_versions": [
            event.get("provider_version") for event in events if event["kind"] == "session.start"
        ],
        "submissions": [
            {
                "active_turn_id": event.get("active_turn_id"),
                "origin": event.get("origin"),
                "wait": event.get("wait"),
            }
            for event in events
            if event["kind"] == "prompt.submit"
        ],
        "calls": calls,
        "authorization": "not_established",
        "missing_contract": "authenticated input receipt and actual consumption binding",
    }


def main() -> None:
    """Read a fresh probe directory and print its evidence, refusing partial records."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    paths = sorted(args.directory.glob("*.json"))
    if not paths:
        parser.error("no probe records; plugin loading has not been established")
    try:
        events = [json.loads(path.read_text()) for path in paths]
        if any(event.get("version") != 1 for event in events):
            raise ValueError("unsupported probe record version")
        result = summarize(events)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(f"incomplete or malformed probe evidence: {error}")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
