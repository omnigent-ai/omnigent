"""Print the project's canonical version as text or JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import tomllib


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit a JSON object")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    version = tomllib.loads(project.read_text(encoding="utf-8"))["project"]["version"]
    print(json.dumps({"version": version}) if args.json else version)


if __name__ == "__main__":
    main()
