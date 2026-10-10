"""Stand-in for the ``ucode`` / ``ug`` CLI used by the setup workspace-drift e2e test.

The real CLI needs a private git source and Databricks OAuth, so this writes only
what Omnigent reads after ``ug configure``: the workspace entry and
``current_workspace`` in ``~/.ucode/state.json``, plus the ``~/.databrickscfg``
profile.
"""

from __future__ import annotations

import argparse
import configparser
import json
import sys
from pathlib import Path
from urllib.parse import urlparse


def _cfg_path() -> Path:
    return Path.home() / ".databrickscfg"


def _state_path() -> Path:
    return Path.home() / ".ucode" / "state.json"


def _read_cfg() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(_cfg_path())
    return cfg


def _save_profile(url: str, requested: str | None) -> str:
    """Return the profile serving *url*, creating ``requested`` (or a host-derived name)."""
    cfg = _read_cfg()
    for section in cfg.sections():
        host = cfg.get(section, "host", fallback="").rstrip("/")
        if host == url and requested in (None, section):
            return section
    name = requested or urlparse(url).netloc.split(".")[0]
    cfg[name] = {"host": url, "auth_type": "databricks-cli"}
    _cfg_path().parent.mkdir(parents=True, exist_ok=True)
    with _cfg_path().open("w") as handle:
        cfg.write(handle)
    return name


def configure(targets: list[tuple[str, str | None]], agents: list[str]) -> int:
    """Record each ``(workspace url, requested profile)`` and make the last one current."""
    path = _state_path()
    state = json.loads(path.read_text()) if path.exists() else {}
    workspaces = state.get("workspaces")
    if not isinstance(workspaces, dict):
        workspaces = {}
    for url, requested in targets:
        url = url.rstrip("/")
        name = _save_profile(url, requested)
        print(f"Select workspace: {name}  {url}")
        print(f"Profile {name} was successfully saved")
        workspaces[url] = {
            "workspace": url,
            "available_tools": agents,
            "agents": {
                agent: {"auth_command": f"databricks auth token --host {url} --profile {name}"}
                for agent in agents
            },
        }
        state["current_workspace"] = url
        print(f"Configuration\n  Workspace: {url}\n  Coding Agents: {', '.join(agents)}")
    state["workspaces"] = workspaces
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")
    print("Configuration complete - launch with ug.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ug", description="fake ucode for tests")
    sub = parser.add_subparsers(dest="command", required=True)
    conf = sub.add_parser("configure")
    # The two real invocations: ``--workspaces <urls>`` from interactive setup and
    # ``--profiles <names>`` from the headless sandbox boot.
    conf.add_argument("--workspaces", default=None)
    conf.add_argument("--profiles", default=None)
    conf.add_argument("--agents", default="claude,codex,pi")
    conf.add_argument("--profile", default=None)
    for flag in (
        "--enable-fable",
        "--skip-validate",
        "--skip-upgrade",
        "--skip-unavailable",
        "--use-pat",
    ):
        conf.add_argument(flag, action="store_true")
    args = parser.parse_args(argv)
    agents = [a for a in args.agents.split(",") if a]
    targets: list[tuple[str, str | None]] = []
    if args.profiles:
        cfg = _read_cfg()
        for name in args.profiles.split(","):
            host = cfg.get(name, "host", fallback=None) if name else None
            if not host:
                parser.error(f"profile {name!r} has no host in ~/.databrickscfg")
            targets.append((host, name))
    elif args.workspaces:
        targets = [(u, args.profile) for u in args.workspaces.split(",") if u]
    else:
        parser.error("configure needs --workspaces or --profiles")
    return configure(targets, agents)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
