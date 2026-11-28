"""Read collected events in evidence tests without importing another test module."""

import json


def events(path):
    return [
        json.loads(line)
        for file in path.glob("events-*.jsonl")
        for line in file.read_text().splitlines()
    ]
