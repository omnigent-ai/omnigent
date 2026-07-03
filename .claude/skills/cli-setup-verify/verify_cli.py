#!/usr/bin/env python3
"""Drive the Omnigent CLI through a PTY in a throwaway sandbox and verify it.

This is the reusable engine behind the ``cli-setup-verify`` skill (see
``SKILL.md`` next to this file for the playbook and CUJ catalog). One run:

1. Builds an **isolated config/data sandbox** so nothing the CLI writes ever
   lands in the real ``~/.omnigent`` — it sets the purpose-built
   ``OMNIGENT_CONFIG_HOME`` / ``OMNIGENT_DATA_DIR`` knobs (``omnigent/cli.py``
   ``_CONFIG_HOME_ENV_VAR`` / ``_DATA_DIR_ENV_VAR``), strips leaked model
   credentials from the child env, and (optionally) points ``HOME`` and a
   minimal ``PATH`` at the sandbox to simulate a brand-new machine.
2. Drives the real ``omnigent`` binary through ``pexpect`` (a real PTY with a
   sane ``TERM`` so prompt-toolkit / the raw-termios pickers actually render).
3. Captures ANSI-stripped frames into an artifacts dir for UX inspection.
4. Runs the named scenario's assertions and prints a single machine-readable
   ``SUMMARY {json}`` line; exits non-zero on failure.
5. Proves it left the real ``~/.omnigent`` byte-for-byte unchanged.

The point is a **verifiable loop**: run a scenario on the *unfixed* code
(``--label before``) to capture the baseline, make the change, run the same
scenario again (``--label after``), and diff the two SUMMARY lines. If you
cannot reach the surface under test (missing harness, no credential), the
scenario reports ``skipped`` — never a false ``pass``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkdtemp

try:
    import pexpect
except ImportError:  # pragma: no cover - guidance, not logic
    sys.stderr.write(
        "verify_cli.py needs `pexpect`. Run it with the omnigent project's "
        "venv python (it bundles pexpect), e.g.\n"
        "  <repo>/.venv/bin/python verify_cli.py ...\n"
    )
    raise

# --- PTY constants (mirrors tests/e2e/omnigent/_pexpect_harness.py) ---------

# prompt-toolkit refuses to draw on TERM=dumb; this is what the REPL tests use.
TERM = "xterm-256color"
# 80x24 is the default new-user window — exactly where narrow-terminal bugs
# (banner overflow, picker redraw past the bottom row) show up. Override with
# --cols/--rows to also exercise the roomy 120x40 layout.
DEFAULT_COLS = 80
DEFAULT_ROWS = 24

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# Stable onboarding anchors (omnigent/cli.py:520, :10064, :10300).
ANCHOR_SEARCHING = "Searching for existing credentials"
ANCHOR_CONFIGURE = "Configure harnesses"
ANCHOR_NO_HARNESS = "Found no harnesses configured"
# REPL readiness signals (the toolbar state line, with the input prompt as a
# fallback for PTY combos that suppress the bottom toolbar).
REPL_READY = [r"state: sleeping", r"❯ "]

# Keys for driving the raw-termios + prompt-toolkit pickers.
KEY_UP = "\x1b[A"
KEY_DOWN = "\x1b[B"
KEY_ENTER = "\r"
KEY_ESC = "\x1b"

# Model-provider credentials we strip from the child env so a "cold" sandbox
# is genuinely credential-free (the CLI auto-adopts ambient keys otherwise).
LEAKED_CRED_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "CLAUDE_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "CURSOR_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "DATABRICKS_TOKEN",
    "DATABRICKS_HOST",
    "DATABRICKS_CONFIG_PROFILE",
)


def strip_ansi(text: str) -> str:
    """Remove ANSI control sequences so frames can be asserted as plain text."""
    return ANSI_RE.sub("", text)


# --- sandbox ----------------------------------------------------------------


@dataclass
class Sandbox:
    """A throwaway config/data/home for one verification run.

    :param root: Temp directory holding ``config/``, ``data/`` and (unless
        ``--inherit-home``) ``home/``. Removed on cleanup unless ``--keep-sandbox``.
    :param env: The child-process environment with the isolation knobs set.
    :param home_isolated: Whether ``HOME`` was redirected into the sandbox.
    """

    root: Path
    env: dict[str, str]
    home_isolated: bool


def build_sandbox(
    *,
    keep_env_creds: bool,
    inherit_home: bool,
    strip_path: bool,
    omnigent_bin: Path,
) -> Sandbox:
    """Create an isolated sandbox env that cannot touch the real ``~/.omnigent``.

    ``HOME`` is redirected into the sandbox **by default**. This is load-bearing,
    not cosmetic: the CLI's diagnostics logger writes a per-invocation
    ``cli-*.log`` under ``state_dir()`` which is hardcoded to ``Path.home() /
    ".omnigent"`` (``omnigent_ui_sdk/terminal/_config.py``) and ignores
    ``OMNIGENT_CONFIG_HOME`` / ``OMNIGENT_DATA_DIR``. So redirecting ``HOME`` is
    the *only* thing that keeps non-help commands (``config list``, the setup
    PTY spawns, ``server stop`` teardown) from writing into the real home.

    :param keep_env_creds: Keep ambient model keys (e.g. ``ANTHROPIC_API_KEY``)
        in the child env. Default False → a genuinely cold, credential-free run.
    :param inherit_home: Opt OUT of home isolation — use the real ``HOME`` (and
        thus its ambient ``~/.claude`` / ``~/.databrickscfg`` auth). Needed to
        reach a real credentialed REPL, but **relaxes the safety guarantee**:
        non-help commands will then write ``cli-*.log`` into the real
        ``~/.omnigent/logs`` (the broadened fingerprint catches this).
    :param strip_path: Reduce ``PATH`` to just the omnigent binary's dir + an
        empty dir, so node/npm/tmux/claude/codex read as "not installed" — i.e.
        a brand-new machine.
    :param omnigent_bin: Path to the ``omnigent`` console script being driven.
    :returns: A :class:`Sandbox`.
    """
    root = Path(mkdtemp(prefix="omnigent-verify-"))
    (root / "config").mkdir()
    (root / "data").mkdir()

    env = dict(os.environ)
    if not keep_env_creds:
        for var in LEAKED_CRED_VARS:
            env.pop(var, None)

    env["OMNIGENT_CONFIG_HOME"] = str(root / "config")
    env["OMNIGENT_DATA_DIR"] = str(root / "data")
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"  # keep the update nag out of frames
    env["TERM"] = TERM
    env["COLUMNS"] = str(DEFAULT_COLS)
    env["LINES"] = str(DEFAULT_ROWS)

    if not inherit_home:
        home = root / "home"
        home.mkdir()
        env["HOME"] = str(home)

    if strip_path:
        empty = root / "emptybin"
        empty.mkdir()
        env["PATH"] = f"{omnigent_bin.parent}:{empty}"

    return Sandbox(root=root, env=env, home_isolated=not inherit_home)


def fingerprint_real_config() -> dict[str, str]:
    """Fingerprint the real ``~/.omnigent`` so we can prove we never wrote to it.

    Stat-only (size + mtime, no content reads). It captures two things, both
    cheap:

    * the top-level config files (``*.yaml`` / ``*.json`` / ``*.toml`` plus the
      known names) — what onboarding writes; and
    * the set of ``logs/cli-*.log`` diagnostic files — what *any* non-help CLI
      invocation writes via the hardcoded ``Path.home()/.omnigent`` state dir.
      A new ``cli-*.log`` basename after the run means we wrote into the real
      home (the precise violation that slips through ``OMNIGENT_CONFIG_HOME`` /
      ``OMNIGENT_DATA_DIR``). With home isolation on (the default) none appear;
      under ``--inherit-home`` they do — and this is what trips the guard.

    It deliberately does **not** read the multi-GB ``logs/*.log`` bodies,
    ``db-backups/`` or native-state dirs (reading them would hang, and other
    running omnigent daemons churn them → false alarms). The single ``logs/``
    glob is bounded by the diagnostics log cap.

    :returns: Mapping of relative path → ``"<size>:<mtime_ns>"`` (config files)
        or ``"<mtime_ns>"`` (cli logs). Empty if the directory does not exist.
    """
    base = Path.home() / ".omnigent"
    out: dict[str, str] = {}
    if not base.exists():
        return out
    candidates: set[Path] = set()
    for pattern in ("*.yaml", "*.yml", "*.json", "*.toml"):
        candidates.update(base.glob(pattern))
    for name in ("config.yaml", "secrets.json", "auth_tokens.json", "providers.yaml"):
        candidates.add(base / name)
    for p in sorted(candidates):
        if p.is_file():
            st = p.stat()
            out[p.name] = f"{st.st_size}:{st.st_mtime_ns}"
    logs = base / "logs"
    if logs.is_dir():
        for p in sorted(logs.glob("cli-*.log")):
            with contextlib.suppress(OSError):
                out[f"logs/{p.name}"] = str(p.stat().st_mtime_ns)
    return out


# --- result model -----------------------------------------------------------


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Result:
    scenario: str
    label: str
    status: str = "pass"  # pass | fail | skipped
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))
        if not ok and self.status == "pass":
            self.status = "fail"

    def skip(self, reason: str) -> None:
        self.status = "skipped"
        self.notes.append(reason)

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario": self.scenario,
            "label": self.label,
            "status": self.status,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
            "notes": self.notes,
            "artifacts": self.artifacts,
        }


# --- frame capture ----------------------------------------------------------


def drain(child: pexpect.spawn, *, seconds: float) -> str:
    """Read everything the child renders for ``seconds`` and return it raw.

    Used to capture a settled screen (a menu, a help body) without depending on
    a specific completion marker.
    """
    buf: list[str] = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            chunk = child.read_nonblocking(size=4096, timeout=0.3)
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            break
        if chunk:
            buf.append(chunk)
    return "".join(buf)


def save_frame(result: Result, artifacts: Path, name: str, raw: str) -> None:
    """Persist a raw + ANSI-stripped frame and register it on the result."""
    artifacts.mkdir(parents=True, exist_ok=True)
    stripped = strip_ansi(raw)
    (artifacts / f"{name}.ansi.txt").write_text(raw, encoding="utf-8")
    (artifacts / f"{name}.txt").write_text(stripped, encoding="utf-8")
    result.artifacts.append(str(artifacts / f"{name}.txt"))


# --- scenarios --------------------------------------------------------------


def scenario_check_isolation(args, sandbox: Sandbox, result: Result) -> None:
    """Smoke-test the sandbox: a read-only CLI call must not touch real config.

    Runs ``omnigent config list`` inside the sandbox (no PTY needed) and
    asserts (a) it executed, (b) the sandbox config home is now used, (c) the
    real ``~/.omnigent`` fingerprint is unchanged. This is the first thing to
    run to trust every other scenario.
    """
    proc = subprocess.run(
        [str(args.omnigent), "config", "list"],
        env=sandbox.env,
        cwd=str(args.repo),
        capture_output=True,
        text=True,
        timeout=args.timeout,
    )
    save_frame(result, Path(args.artifacts), "config_list", proc.stdout + proc.stderr)
    result.add("config_list_ran", proc.returncode == 0, f"exit={proc.returncode}")
    # The sandbox config home should exist; the real one is checked globally in
    # main() via the before/after fingerprint.
    result.add(
        "sandbox_config_home_used",
        Path(sandbox.env["OMNIGENT_CONFIG_HOME"]).exists(),
        sandbox.env["OMNIGENT_CONFIG_HOME"],
    )


def scenario_cold_start(args, sandbox: Sandbox, result: Result) -> None:
    """Spawn the first-time setup surface a brand-new user sees and capture it.

    Home isolation is on by default (so this is already a fresh machine for
    credentials); add ``--strip-path`` to also make node/tmux/claude read as
    not installed. Asserts the onboarding surface renders (the credential search
    banner or the ``Configure harnesses`` menu), saves the frame for UX review,
    then aborts cleanly.
    """
    child = pexpect.spawn(
        str(args.omnigent),
        ["setup"],
        env=sandbox.env,
        cwd=str(args.repo),
        encoding="utf-8",
        timeout=args.timeout,
        dimensions=(args.rows, args.cols),
    )
    try:
        idx = child.expect(
            [ANCHOR_CONFIGURE, ANCHOR_SEARCHING, ANCHOR_NO_HARNESS, pexpect.EOF],
            timeout=args.timeout,
        )
    except pexpect.TIMEOUT:
        save_frame(result, Path(args.artifacts), "cold_start_timeout", child.before or "")
        result.add("onboarding_rendered", False, "no onboarding anchor within timeout")
        _kill_tree(child)
        return

    pre = child.before or ""
    # Let the menu settle so the captured frame holds the whole harness list.
    settle = drain(child, seconds=2.0)
    frame = pre + (child.after or "") + settle
    save_frame(result, Path(args.artifacts), "cold_start", frame)

    result.add("onboarding_rendered", idx in (0, 1, 2), f"anchor_index={idx}")
    stripped = strip_ansi(frame)
    menu_present = ANCHOR_CONFIGURE in stripped
    result.add(
        "harness_menu_present",
        menu_present,
        "'Configure harnesses' title shown" if menu_present else "menu title missing",
    )
    # Informational UX probe (does NOT fail the run): is there any guided
    # "recommended / start here" affordance, or just a wall of options? This is
    # the cold-start dead-end finding — a fix should flip this note.
    has_recommendation = bool(
        re.search(r"recommend|start here|new here|get started", stripped, re.I)
    )
    result.notes.append(
        f"guided_default_affordance={'present' if has_recommendation else 'absent'}"
    )
    _abort_picker(child)
    _kill_tree(child)


def scenario_setup_snapshot(args, sandbox: Sandbox, result: Result) -> None:
    """Capture the setup menu, then optionally arrow-navigate and snapshot each
    frame, for picker UX review (markers, footer hints, alignment, width).

    Use ``--nav-down N`` to step down N rows capturing a frame each time.
    """
    child = pexpect.spawn(
        str(args.omnigent),
        ["setup"],
        env=sandbox.env,
        cwd=str(args.repo),
        encoding="utf-8",
        timeout=args.timeout,
        dimensions=(args.rows, args.cols),
    )
    try:
        child.expect([ANCHOR_CONFIGURE, ANCHOR_SEARCHING], timeout=args.timeout)
    except pexpect.TIMEOUT:
        result.add("menu_rendered", False, "setup menu did not render")
        _kill_tree(child)
        return
    frame = (child.before or "") + (child.after or "") + drain(child, seconds=1.5)
    save_frame(result, Path(args.artifacts), "setup_menu_0", frame)
    result.add("menu_rendered", ANCHOR_CONFIGURE in strip_ansi(frame))

    for i in range(1, args.nav_down + 1):
        child.send(KEY_DOWN)
        frame = drain(child, seconds=1.0)
        save_frame(result, Path(args.artifacts), f"setup_menu_{i}", frame)

    _abort_picker(child)
    _kill_tree(child)


def scenario_help_snapshot(args, sandbox: Sandbox, result: Result) -> None:
    """Render ``omnigent [SUBCOMMAND] --help`` and lint it for known UX issues.

    No PTY needed. The lint checks map directly to top-20 findings, so a fix is
    verifiable as a before/after flip:
      * ``no_param_leak``    — no ``:param``/``:returns`` Sphinx dump (finding X3)
      * ``no_update_dup``    — top-level help doesn't list both update & upgrade (X2)
    Use ``--subcommand server`` (etc.) to lint a specific command's help.
    """
    cmd = [str(args.omnigent)]
    if args.subcommand:
        cmd.append(args.subcommand)
    cmd.append("--help")
    proc = subprocess.run(
        cmd,
        env={**sandbox.env, "COLUMNS": str(args.cols)},
        cwd=str(args.repo),
        capture_output=True,
        text=True,
        timeout=args.timeout,
    )
    out = proc.stdout + proc.stderr
    label = args.subcommand or "root"
    save_frame(result, Path(args.artifacts), f"help_{label}", out)
    result.add(
        "help_rendered",
        proc.returncode == 0 and "Usage:" in out,
        f"exit={proc.returncode}",
    )
    param_leak = ":param" in out or ":returns:" in out
    result.add(
        "no_param_leak",
        not param_leak,
        "Sphinx :param/:returns leaked into --help" if param_leak else "clean",
    )
    if not args.subcommand:
        both = "\n  update" in out and "\n  upgrade" in out
        result.add(
            "no_update_dup",
            not both,
            "both `update` and `upgrade` listed (duplicate)"
            if both
            else "single canonical upgrade",
        )
        cmd_count = len(re.findall(r"^  [a-z][\w-]+\s{2,}", out, re.M))
        result.notes.append(f"top_level_command_count={cmd_count}")


def scenario_repl_commands(args, sandbox: Sandbox, result: Result) -> None:
    """Boot the REPL and check command discoverability.

    Asserts the ``/help`` command list renders; separately records whether
    ``/quit`` is advertised (the ``quit_advertised`` note — finding U2).

    Requires a working harness + credential to reach the prompt: pass
    ``--inherit-home`` (for ambient ``~/.claude`` auth) and/or
    ``--keep-env-creds`` (for an env API key) plus an ``--agent``/``--harness``.
    If the prompt is not reachable the scenario reports ``skipped`` (never a
    false pass).
    """
    if not args.agent:
        result.skip("repl-commands needs --agent <dir/yaml> (and a working harness/credential)")
        return
    spawn_args = ["run", args.agent, "--harness", args.harness]
    if args.model:
        spawn_args += ["--model", args.model]
    child = pexpect.spawn(
        str(args.omnigent),
        spawn_args,
        env=sandbox.env,
        cwd=str(args.repo),
        encoding="utf-8",
        timeout=args.timeout,
        dimensions=(args.rows, args.cols),
    )
    try:
        child.expect(REPL_READY, timeout=args.timeout)
    except (pexpect.TIMEOUT, pexpect.EOF):
        save_frame(result, Path(args.artifacts), "repl_boot_fail", child.before or "")
        result.skip("REPL prompt not reachable (missing harness/credential?) — see repl_boot_fail")
        _kill_tree(child)
        return
    child.send("/help")
    child.send(KEY_ENTER)
    frame = drain(child, seconds=2.5)
    save_frame(result, Path(args.artifacts), "repl_help", frame)
    stripped = strip_ansi(frame)
    # The /help command list rendered (the `/help` row is always present). Note
    # `/quit` discoverability separately — finding U2 is that it is NOT
    # advertised, so a fix flips quit_advertised no→yes.
    result.add("help_lists_commands", "/help" in stripped, "/help output")
    result.notes.append(
        f"quit_advertised={'yes' if '/quit' in stripped else 'no'}"  # discoverability finding U2
    )
    child.send("/quit")
    child.send(KEY_ENTER)
    with contextlib.suppress(Exception):
        child.expect(pexpect.EOF, timeout=10)
    _kill_tree(child)


SCENARIOS = {
    "check-isolation": scenario_check_isolation,
    "cold-start": scenario_cold_start,
    "setup-snapshot": scenario_setup_snapshot,
    "help-snapshot": scenario_help_snapshot,
    "repl-commands": scenario_repl_commands,
}


# --- teardown helpers -------------------------------------------------------


def _abort_picker(child: pexpect.spawn) -> None:
    """Send the menu's abort gestures (q, then Esc) so it exits cleanly."""
    with contextlib.suppress(Exception):
        child.send("q")
        time.sleep(0.2)
        child.send(KEY_ESC)
        time.sleep(0.2)


def _descendant_pids(root_pid: int) -> list[int]:
    """Collect the full descendant tree of ``root_pid`` via repeated ``pgrep -P``.

    Walks children, grandchildren, etc. — a spawned server/runner can re-parent
    its own children, so a single ``pgrep -P`` only reaches one level.
    """
    found: list[int] = []
    frontier = [root_pid]
    seen = {root_pid}
    while frontier:
        parent = frontier.pop()
        with contextlib.suppress(Exception):
            out = subprocess.run(
                ["pgrep", "-P", str(parent)], capture_output=True, text=True
            ).stdout
            for tok in out.split():
                with contextlib.suppress(ValueError):
                    pid = int(tok)
                    if pid not in seen:
                        seen.add(pid)
                        found.append(pid)
                        frontier.append(pid)
    return found


def _kill_tree(child: pexpect.spawn) -> None:
    """Force-kill the child and its whole descendant tree; never raise."""
    pid = child.pid
    # Snapshot descendants BEFORE close() — closing the PTY can reparent them to
    # init, after which pgrep -P can no longer find them via the child.
    descendants = _descendant_pids(pid) if pid else []
    with contextlib.suppress(Exception):
        child.close(force=True)
    for dpid in descendants:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(dpid, signal.SIGKILL)


def stop_sandbox_server(args, sandbox: Sandbox) -> None:
    """Best-effort: stop any background server bound to the sandbox data dir."""
    with contextlib.suppress(Exception):
        subprocess.run(
            [str(args.omnigent), "server", "stop"],
            env=sandbox.env,
            cwd=str(args.repo),
            capture_output=True,
            text=True,
            timeout=30,
        )


# --- main -------------------------------------------------------------------


def resolve_omnigent(repo: Path, explicit: str | None) -> Path:
    """Find the ``omnigent`` console script to drive."""
    if explicit:
        return Path(explicit).resolve()
    venv = repo / ".venv" / "bin" / "omnigent"
    if venv.exists():
        return venv.resolve()
    found = shutil.which("omnigent")
    if found:
        return Path(found).resolve()
    sys.exit("Could not find an `omnigent` binary; pass --omnigent <path>.")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--scenario", choices=sorted(SCENARIOS), help="Scenario to run.")
    p.add_argument("--list-scenarios", action="store_true", help="List scenarios and exit.")
    p.add_argument(
        "--repo",
        default=os.getcwd(),
        type=lambda s: Path(s).resolve(),
        help="Repo root (child cwd).",
    )
    p.add_argument(
        "--omnigent",
        help="Path to the omnigent binary (default: <repo>/.venv/bin/omnigent or PATH).",
    )
    p.add_argument(
        "--label",
        default="run",
        help="Label for this run, e.g. before/after, in the SUMMARY line.",
    )
    p.add_argument("--artifacts", help="Dir for captured frames (default: <sandbox>/artifacts).")
    p.add_argument("--cols", type=int, default=DEFAULT_COLS, help="PTY columns (default 80).")
    p.add_argument("--rows", type=int, default=DEFAULT_ROWS, help="PTY rows (default 24).")
    p.add_argument("--timeout", type=float, default=60.0, help="Per-expect timeout seconds.")
    p.add_argument(
        "--inherit-home",
        action="store_true",
        help="Opt out of HOME isolation (use real HOME + ambient auth). "
        "Less safe: non-help commands then write cli-*.log into the real "
        "~/.omnigent/logs. Use only to reach a real credentialed REPL.",
    )
    p.add_argument(
        "--strip-path",
        action="store_true",
        help="Minimal PATH so node/tmux/claude read as not installed.",
    )
    p.add_argument(
        "--keep-env-creds",
        action="store_true",
        help="Keep ambient model API keys in the child env.",
    )
    p.add_argument(
        "--keep-sandbox",
        action="store_true",
        help="Do not delete the sandbox (for inspection).",
    )
    p.add_argument(
        "--nav-down",
        type=int,
        default=0,
        help="(setup-snapshot) arrow-down N times, capturing each frame.",
    )
    p.add_argument(
        "--subcommand",
        help="(help-snapshot) subcommand to lint, e.g. server. Omit for top-level.",
    )
    p.add_argument("--agent", help="(repl-commands) agent dir/yaml to run.")
    p.add_argument("--harness", default="claude-sdk", help="(repl-commands) harness.")
    p.add_argument("--model", help="(repl-commands) model override.")
    return p.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    if args.list_scenarios:
        for name, fn in sorted(SCENARIOS.items()):
            print(f"{name:16} {(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0
    if not args.scenario:
        sys.exit("Pass --scenario <name> (or --list-scenarios).")

    args.omnigent = resolve_omnigent(args.repo, args.omnigent)
    sandbox = build_sandbox(
        keep_env_creds=args.keep_env_creds,
        inherit_home=args.inherit_home,
        strip_path=args.strip_path,
        omnigent_bin=args.omnigent,
    )
    if not args.artifacts:
        args.artifacts = str(sandbox.root / "artifacts")

    before = fingerprint_real_config()
    result = Result(scenario=args.scenario, label=args.label)
    try:
        SCENARIOS[args.scenario](args, sandbox, result)
    except Exception as exc:  # noqa: BLE001 - report any scenario error as a failed check, never crash the loop
        result.add("scenario_exception", False, f"{type(exc).__name__}: {exc}")
    finally:
        stop_sandbox_server(args, sandbox)

    after = fingerprint_real_config()
    untouched = before == after
    result.add(
        "real_config_untouched",
        untouched,
        "~/.omnigent unchanged" if untouched else "REAL CONFIG MUTATED — investigate",
    )

    if not args.keep_sandbox:
        shutil.rmtree(sandbox.root, ignore_errors=True)
    else:
        result.notes.append(f"sandbox_kept={sandbox.root}")

    print("SUMMARY " + json.dumps(result.to_dict()))
    return 0 if result.status in ("pass", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
