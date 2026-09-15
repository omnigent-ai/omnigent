"""Bridge utilities for native Devin TUI sessions.

The devin-native harness wraps the resident ``devin`` TUI in a runner-owned
tmux pane. Three channels connect it to Omnigent, all rooted in a per-session
*bridge directory*:

* **In** — web-UI messages are pasted into the TUI composer over tmux
  (:func:`inject_user_message`), and slash commands / interrupts use the same
  path (:func:`inject_slash_command`, :func:`inject_interrupt`).
* **Out** — Devin's lifecycle hooks append their stdin payloads to
  ``hooks.jsonl`` (:func:`record_hook_event`), which
  :mod:`omnigent.harnesses.devin_native.forwarder` tails and republishes as Omnigent
  conversation items.
* **Gate** — the same hook subprocess POSTs ``PreToolUse`` /
  ``UserPromptSubmit`` to the server's policy endpoint and mirrors Devin's own
  ``PermissionRequest`` prompts into the web UI (see
  :mod:`omnigent.harnesses.devin_native.hook`).

Devin reads its user-level settings from ``~/.config/devin/config.json`` and
accepts ``--config <path>`` to point somewhere else. Rather than writing hooks
into the user's repository (``.devin/hooks.v1.json`` would be a tracked file),
:func:`write_devin_session_config` merges the user's own config with an
Omnigent ``hooks`` block into a session-scoped file inside the bridge dir. The
user's settings survive, the repo stays clean, and the hooks die with the
session.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from omnigent._platform import stable_user_id
from omnigent.util.json_types import JsonObject as _JsonObject

DEVIN_NATIVE_BRIDGE_DIR_ENV_VAR = "HARNESS_DEVIN_NATIVE_BRIDGE_DIR"
DEVIN_NATIVE_REQUEST_SESSION_ID_ENV_VAR = "HARNESS_DEVIN_NATIVE_REQUEST_SESSION_ID"

_BRIDGE_ROOT = Path(tempfile.gettempdir()) / f"omnigent-{stable_user_id()}" / "devin-native"

_TMUX_FILE = "tmux.json"
_HOOKS_FILE = "hooks.jsonl"
#: Stamped on a recorded ``UserPromptSubmit`` whose policy verdict blocked it.
#: Devin never runs a blocked prompt, so no ``Stop`` follows — the forwarder
#: reads this to avoid mirroring a turn that never happened.
DEVIN_POLICY_BLOCKED_KEY = "omnigent_policy_blocked"
_FORWARDER_READY_FILE = "devin_forwarder_ready.json"
#: Session-scoped ``--config`` file (user config + Omnigent hooks).
_SESSION_CONFIG_FILE = "devin_config.json"
#: Token file the shared ``serve-mcp`` reads at boot.
_MCP_BRIDGE_CONFIG_FILE = "bridge.json"
#: Name Devin lists the Omnigent relay under (``devin mcp list``).
_MCP_SERVER_NAME = "omnigent"
#: ``0o700`` shell wrapper every hook is launched as; bakes the server URL,
#: session id and one-shot auth headers so the hook itself stays import-light.
_HOOK_WRAPPER_FILE = "devin_hook.sh"
#: Where ``devin --export`` writes the ATIF transcript (reasoning + metrics).
_EXPORT_FILE = "transcript.atif.json"

_PASTE_BUFFER = "omnigent-devin-paste"

#: Devin's permission modes, in increasing autonomy. Passed through as
#: ``--permission-mode``. Declared here (a stdlib-only leaf) rather than in
#: :mod:`omnigent.harnesses.devin_native.main` so the CLI can use them in a ``click.Choice`` at
#: decorator time without importing the launcher stack.
DEVIN_PERMISSION_MODES: tuple[str, ...] = (
    "normal",
    "auto",
    "accept-edits",
    "smart",
    "dangerous",
    "bypass",
)

#: Effort rungs Devin encodes as a model-variant suffix (``claude-opus-5-xhigh``).
#: Matches :data:`omnigent.util.reasoning_effort.ANTHROPIC_EFFORTS`, which is the
#: ladder Devin's flagship families expose.
DEVIN_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

_TMUX_READY_TIMEOUT_S = 30.0
#: Devin renders its banner, model row and composer before it is interactive.
#: A cold start behind a slow network measured ~20s, so keep waiting past the
#: normal gate while the pane is provably still coming up.
_DEVIN_BOOT_READY_TIMEOUT_S = 120.0
_TMUX_SEND_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.2
_TYPE_SETTLE_S = 0.3
_TYPE_COMMIT_TIMEOUT_S = 5.0
_SUBMIT_VERIFY_TIMEOUT_S = 5.0
_SUBMIT_RETRY_INTERVAL_S = 0.5
#: Devin's own hint is "esc twice to interrupt" — one Escape only clears the
#: composer draft, so a single key leaves the turn running.
_INTERRUPT_KEY_INTERVAL_S = 0.4

_DEVIN_SEPARATOR = "────"
#: Composer placeholder while Devin is idle (ready for a new turn).
_DEVIN_IDLE_PLACEHOLDER = "Ask Devin to build features, fix bugs, or work on your code"
#: Composer placeholder while a turn is in flight. Devin still accepts input
#: then (it steers the running turn), so both placeholders mean "injectable".
_DEVIN_BUSY_PLACEHOLDER = "Guide Devin while it works"
_DEVIN_INPUT_READY_MARKERS = (_DEVIN_IDLE_PLACEHOLDER, _DEVIN_BUSY_PLACEHOLDER)
#: Pane text shown while the TUI is still starting up.
_DEVIN_BOOT_MARKERS = ("Starting", "Loading", "Connecting")
#: Devin parks a message submitted mid-turn in its own queue and offers this
#: hint; pressing Enter again is its "send now".
_DEVIN_SEND_NOW_HINT = "send now"
#: Devin marks a non-default permission mode on the composer's top rule, e.g.
#: ``── (accept edits on) ──``; the default (``normal``) shows no marker. Captured
#: from devin 3000.10.21 while cycling with Shift+Tab.
_DEVIN_PERMISSION_MARKERS: tuple[tuple[str, str], ...] = (
    ("(bypass permissions on)", "dangerous"),
    ("(accept edits on)", "accept-edits"),
    ("(smart mode on)", "smart"),
)
#: Devin's footer carries the turn's context fill, e.g.
#: ``Context: 25k / 1.0M tokens (2%)``. That is the only place it reports the
#: window, so it is what feeds the web context ring.
_DEVIN_CONTEXT_RE = re.compile(
    r"Context:\s*([\d.]+)\s*([kKmM]?)\s*(?:/|of)\s*([\d.]+)\s*([kKmM]?)"
)
#: Devin's default permission mode — the one with no on-screen marker.
DEVIN_DEFAULT_PERMISSION_MODE = "normal"
#: Aliases Devin accepts for the canonical rungs (from its own validator).
_DEVIN_PERMISSION_ALIASES = {
    "auto": "normal",
    "bypass": "dangerous",
    "yolo": "dangerous",
}
#: Shift+Tab cycles the modes, so a switch is bounded by one full cycle plus slack.
_PERMISSION_CYCLE_MAX_PRESSES = 8
_PERMISSION_SETTLE_S = 0.25
_QUEUE_FLUSH_TIMEOUT_S = 2.0
_QUEUE_FLUSH_INTERVAL_S = 0.3

#: Hook events Omnigent registers. ``PreToolUse`` / ``UserPromptSubmit`` are
#: enforcement gates; ``PermissionRequest`` mirrors Devin's own consent prompt
#: to the web UI; the rest are observational and drive the forwarder.
DEVIN_HOOK_EVENTS: tuple[str, ...] = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
    "PostCompaction",
    "SessionEnd",
)
#: Events whose hook holds a gate open while a human decides. Devin kills a
#: hook at its timeout, so the deciding events get a long one and the
#: observational events a short one.
_GATE_HOOK_EVENTS = frozenset({"PreToolUse", "UserPromptSubmit", "PermissionRequest"})
_GATE_HOOK_TIMEOUT_S = 86_400
_OBSERVER_HOOK_TIMEOUT_S = 30

# Ambient provider/cloud/CI credentials that must not be inherited by Devin.
# Devin authenticates through its own `devin auth login` credential file, so a
# stray vendor key in the environment would silently re-route its traffic.
DEVIN_NATIVE_ENV_UNSET = [
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_CLIENT_SECRET",
    "CI",
    "DATABRICKS_CLIENT_SECRET",
    "DATABRICKS_CONFIG_PROFILE",
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "GEMINI_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
]

_CHILD_ENV_ALLOWLIST = [
    "COLORTERM",
    "DEVIN_CONFIG_HOME",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NO_COLOR",
    "PATH",
    "SHELL",
    "TERM",
    "TERM_PROGRAM",
    "TMPDIR",
    "USER",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
]


def bridge_root() -> Path:
    """Return the uid-scoped devin-native bridge root.

    Mirrors the sibling harnesses' ``bridge_root`` accessor so the shared
    ``serve-mcp`` / relay infrastructure recognizes Devin bridge dirs as a
    trusted root.
    """
    return _BRIDGE_ROOT


def bridge_dir_for_session_id(session_id: str) -> Path:
    """Return the per-session Devin bridge directory."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def prepare_bridge_dir(session_id: str) -> Path:
    """Create and return the per-session Devin bridge directory."""
    bridge_dir = bridge_dir_for_session_id(session_id)
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(bridge_dir, 0o700)
    return bridge_dir


def hooks_path(bridge_dir: Path) -> Path:
    """Return the append-only hook-event log the forwarder tails."""
    return bridge_dir / _HOOKS_FILE


def session_config_path(bridge_dir: Path) -> Path:
    """Return the session-scoped ``devin --config`` file path."""
    return bridge_dir / _SESSION_CONFIG_FILE


def export_path(bridge_dir: Path) -> Path:
    """Return the ``devin --export`` ATIF transcript path."""
    return bridge_dir / _EXPORT_FILE


def hook_wrapper_path(bridge_dir: Path) -> Path:
    """Return the ``0o700`` hook wrapper script path."""
    return bridge_dir / _HOOK_WRAPPER_FILE


def build_devin_native_spawn_env(session_id: str) -> dict[str, str]:
    """Build the ``HARNESS_DEVIN_NATIVE_*`` env for the harness executor."""
    bridge_dir = prepare_bridge_dir(session_id)
    return {
        DEVIN_NATIVE_BRIDGE_DIR_ENV_VAR: str(bridge_dir),
        DEVIN_NATIVE_REQUEST_SESSION_ID_ENV_VAR: session_id,
    }


def build_devin_native_terminal_env(
    session_id: str,
    *,
    source_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the allowlisted child environment for the ``devin`` TUI."""
    env = os.environ if source_env is None else source_env
    child = {key: env[key] for key in _CHILD_ENV_ALLOWLIST if env.get(key)}
    bridge_dir = prepare_bridge_dir(session_id)
    child[DEVIN_NATIVE_BRIDGE_DIR_ENV_VAR] = str(bridge_dir)
    return child


# ---------------------------------------------------------------------------
# Session config (hooks registration)
# ---------------------------------------------------------------------------


def user_config_path(env: Mapping[str, str] | None = None) -> Path:
    """Return Devin's user-level config path, honouring ``XDG_CONFIG_HOME``."""
    env = os.environ if env is None else env
    xdg = env.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path(env.get("HOME", str(Path.home()))) / ".config"
    return base / "devin" / "config.json"


def _strip_jsonc_comments(raw: str) -> str:
    """Drop ``//`` and ``/* */`` comments, leaving string contents untouched."""
    out: list[str] = []
    index = 0
    length = len(raw)
    in_string = False
    while index < length:
        char = raw[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(raw[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length:
            following = raw[index + 1]
            if following == "/":
                while index < length and raw[index] != "\n":
                    index += 1
                continue
            if following == "*":
                index += 2
                while index + 1 < length and not (raw[index] == "*" and raw[index + 1] == "/"):
                    index += 1
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _read_user_config(path: Path) -> _JsonObject:
    """Return the user's Devin config, or ``{}`` when absent/unparseable.

    Devin accepts JSON with ``//`` and ``/* */`` comments, which
    :func:`json.loads` rejects. A config we cannot parse is not fatal — we fall
    back to an empty base so the session still gets its hooks, rather than
    refusing to launch over a stylistic comment.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        # Devin accepts JSONC, so retry without comments before giving up —
        # discarding the whole config would silently drop the user's permissions,
        # org and read_config_from settings that `--config` is meant to preserve.
        try:
            parsed = json.loads(_strip_jsonc_comments(raw))
        except ValueError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def build_hook_config(hook_command: str) -> _JsonObject:
    """Build Devin's ``hooks`` block routing every event to *hook_command*.

    Every event runs the same wrapper; the hook reads ``hook_event_name`` from
    its stdin payload to decide what to do. An empty ``matcher`` matches all
    tools (Devin treats it as "match everything").

    :param hook_command: Absolute path to the hook wrapper script.
    :returns: A ``hooks`` mapping suitable for Devin's config file.
    """
    hooks: _JsonObject = {}
    for event in DEVIN_HOOK_EVENTS:
        timeout = _GATE_HOOK_TIMEOUT_S if event in _GATE_HOOK_EVENTS else _OBSERVER_HOOK_TIMEOUT_S
        entry: _JsonObject = {
            "hooks": [{"type": "command", "command": hook_command, "timeout": timeout}]
        }
        # Tool-scoped events take a matcher; prompt/session events do not.
        if event in {"PreToolUse", "PostToolUse", "PermissionRequest"}:
            entry["matcher"] = ""
        hooks[event] = [entry]
    return hooks


def write_devin_session_config(
    bridge_dir: Path,
    *,
    hook_command: str,
    model: str | None = None,
    source_env: Mapping[str, str] | None = None,
) -> Path:
    """Write the session-scoped Devin config and return its path.

    Merges the user's own ``config.json`` with an Omnigent ``hooks`` block so
    the wrapped TUI keeps the user's theme, permissions and MCP preferences
    while still reporting to Omnigent. ``--config`` replaces only the *user*
    config file, so project-level ``.devin/config.json`` still applies on top.

    :param bridge_dir: Per-session bridge directory.
    :param hook_command: Absolute path to the hook wrapper script.
    :param model: Optional Devin model id to pin as ``agent.model``.
    :param source_env: Environment used to locate the user config (tests).
    :returns: Path to the written session config.
    """
    config = _read_user_config(user_config_path(source_env))
    config["hooks"] = build_hook_config(hook_command)
    if model:
        agent = config.get("agent")
        agent = dict(agent) if isinstance(agent, dict) else {}
        agent["model"] = model
        config["agent"] = agent
    path = session_config_path(bridge_dir)
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


#: Workspace-relative always-on rule file carrying a custom Devin agent's
#: ``AgentSpec.instructions``. Devin has no ``--append-system-prompt`` and
#: ignores config-level instructions (verified), so a Windsurf always-on rule is
#: the only channel that reaches every turn's system prompt. Stable name so each
#: launch overwrites rather than accumulating.
#: Devin keeps MCP servers in a project-local file, not the ``--config`` user
#: config, so the Omnigent relay is registered per workspace (same shape as the
#: agent rule below). Verified against ``devin mcp add`` on 3000.10.21.
_MCP_CONFIG_RELPATH = (".devin", "mcp_config.local.json")


def write_relay_bridge_config(bridge_dir: Path) -> None:
    """Write a token-only ``bridge.json`` so the shared ``serve-mcp`` can boot.

    Carries only a token — no ``workspace`` key, so no ``sys_os_*`` tools are
    served (Devin owns its own filesystem tools); the relay tools themselves come
    from ``tool_relay.json``. Idempotent, so a relaunch never rotates a token the
    relay was already started with. Mirrors the opencode/cursor writers.

    :param bridge_dir: Per-session Devin bridge directory.
    """
    config_path = bridge_dir / _MCP_BRIDGE_CONFIG_FILE
    if config_path.exists():
        return
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = bridge_dir / (_MCP_BRIDGE_CONFIG_FILE + ".tmp")
    tmp.write_text(
        json.dumps({"token": secrets.token_urlsafe(32)}, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(tmp, config_path)


def build_devin_mcp_server(
    bridge_dir: Path,
    *,
    python_executable: str | None = None,
) -> _JsonObject:
    """Build Devin's stdio entry for the shared Omnigent MCP relay.

    :param bridge_dir: Per-session bridge dir the relay serves from.
    :param python_executable: Interpreter to run ``serve-mcp`` with (tests).
    :returns: One ``mcpServers`` entry in Devin's own schema.
    """
    return {
        "command": python_executable or sys.executable,
        "args": [
            "-I",
            "-m",
            "omnigent.harnesses.claude_native.bridge",
            "serve-mcp",
            "--bridge-dir",
            str(bridge_dir),
        ],
        "transport": "stdio",
        "env": {"TMPDIR": os.environ.get("TMPDIR", "/tmp")},
    }


def write_devin_mcp_config(
    workspace: Path,
    bridge_dir: Path,
    *,
    python_executable: str | None = None,
) -> Path:
    """Register the Omnigent relay in ``<workspace>/.devin/mcp_config.local.json``.

    Devin reads MCP servers from this project-local file (``--config`` replaces
    only the *user* config, which carries no MCP), so this is where the relay has
    to land. Merges into any existing file so the user's own servers survive; a
    hand-edited file of any other shape is discarded rather than crashing launch.

    :param workspace: Session workspace (Devin's cwd).
    :param bridge_dir: Per-session bridge dir.
    :param python_executable: Interpreter for the relay command (tests).
    :returns: Path to the written MCP config.
    """
    write_relay_bridge_config(bridge_dir)
    path = workspace.joinpath(*_MCP_CONFIG_RELPATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        loaded = None
    existing: _JsonObject = loaded if isinstance(loaded, dict) else {}
    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        existing["mcpServers"] = servers
    servers[_MCP_SERVER_NAME] = build_devin_mcp_server(
        bridge_dir, python_executable=python_executable
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


_AGENT_RULE_RELPATH = (".windsurf", "rules", "omnigent-agent-instructions.md")
#: Frontmatter key naming the session a rule belongs to, so a launch never
#: deletes or overwrites instructions another session is running under.
_AGENT_RULE_STAMP_KEY = "omnigent_session"

#: A custom agent's instructions, staged for the first injected message when the
#: rule channel is not safe to use for this workspace.
_INSTRUCTIONS_PREAMBLE_FILE = "instructions_preamble.txt"
AGENT_INSTRUCTIONS_OPEN_TAG = "<omnigent_agent_instructions>"
AGENT_INSTRUCTIONS_CLOSE_TAG = "</omnigent_agent_instructions>"
_AGENT_INSTRUCTIONS_HEADER = (
    "These are your operating instructions for this session; follow them "
    "throughout, not just for this message:"
)


#: Prior conversation a forked clone replays on its first message: written by the
#: launch path, consumed once by the executor.
_FORK_PREAMBLE_FILE = "fork_preamble.txt"
#: Sentinel framing that history inside the injected message. Devin reads the
#: framed block as context; the forwarder strips it when mirroring the user turn so
#: the copied conversation is not duplicated in the Omnigent timeline. Same
#: sentinel text cursor-native uses, so a fork reads the same either side.
FORK_HISTORY_OPEN_TAG = "<omnigent_fork_history>"
FORK_HISTORY_CLOSE_TAG = "</omnigent_fork_history>"
# Neutral about *why* the history is replayed: the same block serves a fork and a
# resume with no Devin session to reattach to, and telling a resumed session it
# was forked would have it describe its own past wrongly.
_FORK_HISTORY_HEADER = "Here is the earlier conversation this session continues from:"
_FORK_HISTORY_FOOTER = "That is the end of the carried-over conversation; my message follows."


def write_fork_preamble(bridge_dir: Path, preamble: str) -> None:
    """Stage a forked clone's prior conversation for its first injected message.

    Devin's own session store is read-only to Omnigent, so there is no local
    transcript to rebuild for ``--resume``; replaying the turns as text is the
    closest analog (the same choice cursor-native makes).

    :param bridge_dir: Per-session bridge directory.
    :param preamble: Rendered transcript; blank writes nothing.
    """
    if not preamble.strip():
        return
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (bridge_dir / _FORK_PREAMBLE_FILE).write_text(preamble, encoding="utf-8")


def read_fork_preamble(bridge_dir: Path) -> str | None:
    """Return the staged fork preamble, or ``None`` when there is none.

    Left in place until :func:`clear_fork_preamble`, so a failed first injection
    retries with the history rather than silently losing it.
    """
    try:
        text = (bridge_dir / _FORK_PREAMBLE_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    return text or None


def clear_fork_preamble(bridge_dir: Path) -> None:
    """Drop the staged preamble once it has been delivered."""
    with contextlib.suppress(OSError):
        (bridge_dir / _FORK_PREAMBLE_FILE).unlink()


def _neutralize_fork_sentinels(text: str) -> str:
    """Defang literal sentinel tags so the framed block holds exactly one pair."""
    return text.replace(FORK_HISTORY_OPEN_TAG, "[omnigent_fork_history]").replace(
        FORK_HISTORY_CLOSE_TAG, "[/omnigent_fork_history]"
    )


def wrap_fork_preamble(preamble: str, user_text: str) -> str:
    """Frame the replayed history ahead of the session's first user message.

    :param preamble: Rendered prior-conversation transcript.
    :param user_text: The user's first message in this Devin session.
    :returns: The framed transcript followed by the user text.
    """
    return (
        f"{FORK_HISTORY_OPEN_TAG}\n"
        f"{_FORK_HISTORY_HEADER}\n\n"
        f"{_neutralize_fork_sentinels(preamble)}\n\n"
        f"{_FORK_HISTORY_FOOTER}\n"
        f"{FORK_HISTORY_CLOSE_TAG}\n\n"
        f"{user_text}"
    )


def _rule_dir_is_machine_global(workspace: Path) -> bool:
    """Whether this workspace's rules dir is one Devin reads from every cwd.

    Devin scans ``.windsurf/rules`` under the home directory in addition to the
    cwd, so a session whose workspace *is* the home directory would publish its
    instructions to every Devin invocation on the machine — including the user's
    own, outside Omnigent. Those workspaces take the preamble instead.

    :param workspace: The session's workspace directory.
    :returns: ``True`` when writing the rule would escape the workspace.
    """
    try:
        return workspace.resolve() == Path.home().resolve()
    except OSError:
        return False


def _read_agent_rule(rule_path: Path) -> tuple[str | None, str]:
    """Return ``(owning session id, full file text)`` for an existing rule.

    :param rule_path: The rule file, which need not exist.
    :returns: The stamped session id (``None`` when absent or unstamped) and the
        raw text (``""`` when the file cannot be read).
    """
    try:
        text = rule_path.read_text(encoding="utf-8")
    except OSError:
        return None, ""
    for line in text.splitlines()[:8]:
        key, _, value = line.partition(":")
        if key.strip() == _AGENT_RULE_STAMP_KEY and value.strip():
            return value.strip(), text
    return None, text


def _render_agent_rule(instructions: str, session_id: str) -> str:
    """Render the always-on rule file body for *instructions*."""
    return (
        f"---\ntrigger: always_on\n{_AGENT_RULE_STAMP_KEY}: {session_id}\n---\n"
        f"{instructions.strip()}\n"
    )


def write_devin_agent_rule(workspace: Path, instructions: str | None, *, session_id: str) -> bool:
    """Deliver a custom agent's instructions to Devin as an always-on rule.

    Writes ``<workspace>/.windsurf/rules/omnigent-agent-instructions.md`` with the
    ``trigger: always_on`` frontmatter Devin requires to load a rule into every
    turn's system prompt. Idempotent: called with ``None`` (a plain Devin agent,
    no instructions) it removes any rule a prior custom-agent launch left, so the
    workspace never carries stale instructions.

    :param workspace: The session's workspace directory (Devin reads rules
        relative to its CWD, which is this workspace).
    :param instructions: The verbatim ``AgentSpec.instructions``, or ``None``.
    :param session_id: The Omnigent conversation id, stamped as the rule's owner.
    :returns: ``True`` when the instructions are live in the rule file. ``False``
        means the caller must deliver them another way (see
        :func:`write_agent_instructions_preamble`).
    """
    # ponytail: this writes into the user's workspace — the only always-on
    # channel Devin exposes (rules are CWD-relative; there is no out-of-tree
    # rules dir and config-level instructions are ignored). One fixed filename,
    # because Devin loads EVERY always-on rule in the dir: per-session names
    # would stack one session's instructions onto another's rather than isolate
    # them. Ownership is tracked in frontmatter instead.
    rule_path = workspace.joinpath(*_AGENT_RULE_RELPATH)
    text = instructions.strip() if instructions else ""
    owner, existing = _read_agent_rule(rule_path)

    if not text:
        # Nothing to deliver. Remove only a rule this session owns — an unstamped
        # file predates ownership tracking and is ours by filename; another
        # session's stamp is left alone so its live instructions survive.
        if existing and owner in (None, session_id):
            with contextlib.suppress(OSError):
                rule_path.unlink(missing_ok=True)
        return False

    if _rule_dir_is_machine_global(workspace):
        return False

    rendered = _render_agent_rule(text, session_id)
    if existing == rendered:
        return True
    if owner is not None and owner != session_id and _stamped_session_is_running(owner):
        # A different agent's instructions are live in this shared workspace.
        # Overwriting them would silently re-brief a running session.
        return False
    try:
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        rule_path.write_text(rendered, encoding="utf-8")
    except OSError:
        return False
    return True


def _stamped_session_is_running(session_id: str) -> bool:
    """Whether *session_id* still looks like a live devin-native session.

    The bridge dir is created at launch and removed with the session, so its
    presence is the cheapest cross-process signal available here. Errs toward
    "running", which costs a preamble rather than another session's rule.
    """
    try:
        return bridge_dir_for_session_id(session_id).is_dir()
    except OSError:
        return True


def write_agent_instructions_preamble(bridge_dir: Path, instructions: str) -> None:
    """Stage instructions for the first injected message.

    The fallback for workspaces where the rule channel is not session-scoped.
    Unlike the rule this is not always-on, so it is a weaker delivery — used only
    when the alternative is leaking instructions into other sessions.

    :param bridge_dir: Per-session bridge directory.
    :param instructions: Verbatim agent instructions; blank writes nothing.
    """
    if not instructions.strip():
        return
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (bridge_dir / _INSTRUCTIONS_PREAMBLE_FILE).write_text(instructions, encoding="utf-8")


def read_agent_instructions_preamble(bridge_dir: Path) -> str | None:
    """Return staged instructions, or ``None`` when there are none."""
    try:
        text = (bridge_dir / _INSTRUCTIONS_PREAMBLE_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    return text or None


def clear_agent_instructions_preamble(bridge_dir: Path) -> None:
    """Drop the staged instructions once they have been delivered."""
    with contextlib.suppress(OSError):
        (bridge_dir / _INSTRUCTIONS_PREAMBLE_FILE).unlink()


def wrap_agent_instructions(instructions: str, user_text: str) -> str:
    """Frame staged instructions ahead of the session's first user message.

    :param instructions: Verbatim agent instructions.
    :param user_text: The message text this call prefixes.
    :returns: The framed instructions followed by the user text.
    """
    body = instructions.strip()
    body = body.replace(AGENT_INSTRUCTIONS_OPEN_TAG, "[omnigent_agent_instructions]").replace(
        AGENT_INSTRUCTIONS_CLOSE_TAG, "[/omnigent_agent_instructions]"
    )
    return (
        f"{AGENT_INSTRUCTIONS_OPEN_TAG}\n"
        f"{_AGENT_INSTRUCTIONS_HEADER}\n\n"
        f"{body}\n"
        f"{AGENT_INSTRUCTIONS_CLOSE_TAG}\n\n"
        f"{user_text}"
    )


def write_hook_wrapper(
    bridge_dir: Path,
    *,
    server_url: str,
    session_id: str,
) -> Path:
    """Write the ``0o700`` shell wrapper each Devin hook is launched as.

    Delegates to :func:`omnigent.native.native_policy_hook.policy_hook_wrapper_script`,
    which resolves a one-shot Omnigent bearer and bakes the auth +
    workspace-routing headers into the wrapper's environment. The token is a
    secret, hence ``0o700``.

    :param bridge_dir: Per-session bridge directory.
    :param server_url: Omnigent server base URL the hook POSTs to.
    :param session_id: Omnigent conversation id for policy evaluation.
    :returns: Path to the written wrapper script.
    """
    from omnigent.native.native_policy_hook import policy_hook_wrapper_script

    hook_entry = bridge_dir / "devin_hook_entry.py"
    hook_entry.write_text(
        "import sys\n"
        "from omnigent.harnesses.devin_native.hook import main\n"
        f"sys.exit(main([{str(bridge_dir)!r}]))\n",
        encoding="utf-8",
    )
    script = policy_hook_wrapper_script(server_url, session_id, str(hook_entry))
    path = hook_wrapper_path(bridge_dir)
    path.write_text(script, encoding="utf-8")
    os.chmod(path, 0o700)
    return path


# ---------------------------------------------------------------------------
# Hook event log
# ---------------------------------------------------------------------------


def record_hook_event(bridge_dir: Path, payload: _JsonObject) -> None:
    """Append one hook payload to ``hooks.jsonl`` for the forwarder.

    Line-buffered append with an ``O_APPEND`` write so concurrent hook
    subprocesses (Devin runs them per event, and a tool call fires
    ``PreToolUse`` while a prior ``PostToolUse`` may still be writing) never
    interleave a partial line.

    :param bridge_dir: Per-session bridge directory.
    :param payload: Raw hook JSON as read from the hook's stdin.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    envelope = {"recorded_at": time.time(), "payload": payload}
    line = json.dumps(envelope, ensure_ascii=False) + "\n"
    fd = os.open(hooks_path(bridge_dir), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def iter_hook_events(
    bridge_dir: Path, *, start_offset: int = 0
) -> Iterator[tuple[int, _JsonObject]]:
    """Yield ``(next_offset, payload)`` for hook events from *start_offset*.

    Only whole lines are yielded; a partially-written trailing line is left for
    the next poll, and the returned offset points past the last complete line
    so a caller can resume without re-reading or losing an event.

    :param bridge_dir: Per-session bridge directory.
    :param start_offset: Byte offset to resume from.
    """
    path = hooks_path(bridge_dir)
    try:
        with path.open("rb") as handle:
            handle.seek(start_offset)
            offset = start_offset
            for raw in handle:
                if not raw.endswith(b"\n"):
                    break
                offset += len(raw)
                try:
                    envelope = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(envelope, dict):
                    continue
                payload = envelope.get("payload")
                if isinstance(payload, dict):
                    yield offset, payload
    except OSError:
        return


def hooks_size(bridge_dir: Path) -> int:
    """Return the current byte size of ``hooks.jsonl`` (0 when absent)."""
    try:
        return hooks_path(bridge_dir).stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# tmux plumbing
# ---------------------------------------------------------------------------


def write_tmux_target(
    bridge_dir: Path,
    *,
    socket_path: Path,
    tmux_target: str,
    pid: int | None = None,
    requires_forwarder_ready: bool = False,
) -> None:
    """Advertise the tmux socket + target for the running Devin terminal."""
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: _JsonObject = {
        "socket_path": str(socket_path),
        "tmux_target": tmux_target,
        "updated_at": time.time(),
    }
    if requires_forwarder_ready:
        payload["requires_forwarder_ready"] = True
    if pid is not None:
        payload["pid"] = pid
    tmp = bridge_dir / (_TMUX_FILE + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, bridge_dir / _TMUX_FILE)


def read_tmux_info(bridge_dir: Path) -> dict[str, str] | None:
    """Return ``{socket_path, tmux_target}`` from ``tmux.json``, or ``None``."""
    data = _read_bridge_json(bridge_dir, _TMUX_FILE)
    if data is None:
        return None
    socket_path = data.get("socket_path")
    tmux_target = data.get("tmux_target")
    if (
        isinstance(socket_path, str)
        and socket_path
        and isinstance(tmux_target, str)
        and tmux_target
    ):
        return {"socket_path": socket_path, "tmux_target": tmux_target}
    return None


def write_forwarder_ready(bridge_dir: Path) -> None:
    """Mark the Devin hook forwarder as attached and caught up."""
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"updated_at": time.time()}
    tmp = bridge_dir / (_FORWARDER_READY_FILE + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, bridge_dir / _FORWARDER_READY_FILE)


def _read_bridge_json(bridge_dir: Path, filename: str) -> _JsonObject | None:
    """Return parsed JSON from a bridge file, or ``None`` when unavailable."""
    try:
        raw = (bridge_dir / filename).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _wait_for_tmux_info(bridge_dir: Path, *, timeout_s: float) -> dict[str, str]:
    """Block until the runner advertises the tmux target."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        info = read_tmux_info(bridge_dir)
        if info is not None:
            return info
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(f"devin-native tmux target was not advertised within {timeout_s:.0f}s")


def _wait_for_forwarder_ready_if_required(
    bridge_dir: Path,
    *,
    tmux_info: Mapping[str, object],
    timeout_s: float,
) -> None:
    """Wait for the forwarder when resuming, so replayed turns are not re-posted.

    On a cold resume the forwarder must first walk the existing hook log to
    establish its offset; injecting before that would let the forwarder
    re-publish history as if it were new.
    """
    if not tmux_info.get("requires_forwarder_ready"):
        return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _read_bridge_json(bridge_dir, _FORWARDER_READY_FILE) is not None:
            return
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(f"devin-native forwarder was not ready within {timeout_s:.0f}s")


def _run_tmux(socket_path: str, *args: str) -> None:
    """Run one tmux command against *socket_path*, raising on failure."""
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"tmux command timed out after {_TMUX_SEND_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "<no output>"
        raise RuntimeError(f"tmux command failed (rc={proc.returncode}): {detail}")


def _session_alive(socket_path: str, tmux_target: str) -> bool:
    """Return whether the Devin tmux session still exists."""
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, "has-session", "-t", tmux_target],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _capture_pane(socket_path: str, tmux_target: str) -> str:
    """Capture visible pane contents; return empty string on failure."""
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, "capture-pane", "-p", "-t", tmux_target],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_SEND_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _devin_input_region(pane: str) -> str:
    """Return Devin's bottom composer region, excluding transcript history.

    Devin frames the composer between two horizontal rules and puts the model /
    context status line below the lower rule, so the region is the text between
    the last two ``────`` runs.
    """
    lines = pane.splitlines()
    rules = [index for index, line in enumerate(lines) if _DEVIN_SEPARATOR in line]
    if len(rules) >= 2:
        return "\n".join(lines[rules[-2] + 1 : rules[-1]])
    if rules:
        return "\n".join(lines[rules[-1] + 1 :])
    return "\n".join(lines[-8:])


def devin_input_ready(pane: str) -> bool:
    """Return whether Devin's composer is accepting input.

    True for both the idle and the mid-turn placeholder: Devin takes input
    while a turn runs (it steers the running turn), so a busy composer is still
    injectable.

    Whitespace is collapsed before matching so a placeholder Devin hard-wraps in
    a narrow pane (e.g. the sidebar terminal, which wraps the idle placeholder
    onto two lines) is still recognized — otherwise readiness never trips and the
    turn fails with "composer did not become ready".
    """
    normalized = " ".join(pane.split())
    return any(marker in normalized for marker in _DEVIN_INPUT_READY_MARKERS)


def _devin_still_booting(pane: str) -> bool:
    """Return whether the pane shows Devin still starting up."""
    return any(marker in pane for marker in _DEVIN_BOOT_MARKERS)


def devin_queue_pending(pane: str) -> bool:
    """Return whether Devin is holding a submitted message in its own queue.

    Devin parks a message submitted while a turn runs ("N queued … enter send
    now") instead of steering it in, so a message Omnigent meant to send now
    would sit there until the turn ended.
    """
    normalized = " ".join(pane.split())
    return "queued" in normalized and _DEVIN_SEND_NOW_HINT in normalized


def _scaled_tokens(value: str, suffix: str) -> int | None:
    """Turn Devin's abbreviated token count ("25k", "1.0M") into an int."""
    try:
        parsed = float(value)
    except ValueError:
        return None
    multiplier = {"": 1, "k": 1_000, "K": 1_000, "m": 1_000_000, "M": 1_000_000}.get(suffix)
    if multiplier is None or parsed < 0:
        return None
    return int(parsed * multiplier)


def devin_context_usage(pane: str) -> tuple[int | None, int | None]:
    """Return ``(context tokens used, context window)`` from Devin's footer.

    Devin abbreviates both ("Context: 25k / 1.0M tokens (2%)") and wraps the line
    in a narrow pane, so the text is whitespace-normalized before matching.

    :param pane: Captured pane text.
    :returns: Both values, or ``(None, None)`` when the footer carries no usable
        pair — the ring simply stays hidden rather than showing a wrong fill.
    """
    match = _DEVIN_CONTEXT_RE.search(" ".join(pane.split()))
    if match is None:
        return (None, None)
    used = _scaled_tokens(match.group(1), match.group(2))
    window = _scaled_tokens(match.group(3), match.group(4))
    if used is None or window is None or window <= 0:
        return (None, None)
    return (used, window)


def read_devin_context_usage(bridge_dir: Path) -> tuple[int | None, int | None]:
    """Read the pane's context footer, without waiting on the TUI.

    Best-effort by design: this runs on the forwarder's turn-end path, so a pane
    that has gone away (or a bridge with no tmux target yet) must cost nothing.

    :param bridge_dir: Per-session bridge directory.
    :returns: ``(used, window)``, or ``(None, None)`` when unavailable.
    """
    info = _read_bridge_json(bridge_dir, _TMUX_FILE)
    if not isinstance(info, dict):
        return (None, None)
    socket_path = info.get("socket_path")
    tmux_target = info.get("tmux_target")
    if not isinstance(socket_path, str) or not isinstance(tmux_target, str):
        return (None, None)
    return devin_context_usage(_capture_pane(socket_path, tmux_target))


def canonical_devin_permission_mode(mode: str) -> str:
    """Return *mode* under Devin's canonical spelling.

    Devin accepts aliases (``auto`` for ``normal``, ``bypass``/``yolo`` for
    ``dangerous``), but only the canonical name is what the pane's marker resolves
    to, so a switch has to compare like with like.
    """
    lowered = mode.strip().lower()
    return _DEVIN_PERMISSION_ALIASES.get(lowered, lowered)


def devin_permission_mode(pane: str) -> str:
    """Return the permission mode Devin's composer is currently advertising.

    :param pane: Captured pane text.
    :returns: A canonical mode name; :data:`DEVIN_DEFAULT_PERMISSION_MODE` when no
        marker is present (Devin marks only the non-default modes).
    """
    normalized = " ".join(pane.split())
    for marker, mode in _DEVIN_PERMISSION_MARKERS:
        if marker in normalized:
            return mode
    return DEVIN_DEFAULT_PERMISSION_MODE


def inject_permission_mode(
    bridge_dir: Path,
    *,
    mode: str,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> str:
    """Switch the live Devin session onto *mode* by cycling Shift+Tab.

    Devin has no command that sets a mode directly — its own hint is "Use
    Shift+Tab to cycle permission modes" — so this presses Shift+Tab and re-reads
    the composer's marker after each press, stopping the moment the target shows.
    Verifying every step is what keeps a cycle from overshooting onto
    ``dangerous`` (which auto-approves every tool).

    :param bridge_dir: Per-session bridge directory.
    :param mode: Target mode, canonical or alias.
    :returns: The mode now showing in the pane.
    :raises RuntimeError: If *mode* is unknown, or one full cycle never reached it
        (an older Devin may not offer every rung).
    """
    target = canonical_devin_permission_mode(mode)
    known = {DEVIN_DEFAULT_PERMISSION_MODE, *(m for _marker, m in _DEVIN_PERMISSION_MARKERS)}
    if target not in known:
        raise RuntimeError(f"devin-native does not expose permission mode {mode!r}")
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    _wait_for_devin_input_ready(socket_path, tmux_target, timeout_s=timeout_s)
    for _press in range(_PERMISSION_CYCLE_MAX_PRESSES):
        if devin_permission_mode(_capture_pane(socket_path, tmux_target)) == target:
            return target
        # BTab is tmux's name for Shift+Tab.
        _run_tmux(socket_path, "send-keys", "-t", tmux_target, "BTab")
        time.sleep(_PERMISSION_SETTLE_S)
    settled = devin_permission_mode(_capture_pane(socket_path, tmux_target))
    if settled == target:
        return target
    raise RuntimeError(
        f"devin-native could not reach permission mode {target!r}; the pane is on {settled!r}"
    )


def _flush_devin_queue(socket_path: str, tmux_target: str) -> None:
    """Press Devin's "send now" while it holds a queued message.

    Omnigent delivers a message mid-turn only when the user asked for it to go now
    (the always-steer preference, or the queue strip's send-now), so Devin's own
    queue must not hold it back. Any message the user parked in the pane goes with
    it. Best-effort and bounded: Devin sends a queued message when the turn ends
    anyway, so a pane that will not clear is left alone rather than failed.
    """
    deadline = time.monotonic() + _QUEUE_FLUSH_TIMEOUT_S
    while time.monotonic() < deadline:
        if not devin_queue_pending(_capture_pane(socket_path, tmux_target)):
            return
        _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")
        time.sleep(_QUEUE_FLUSH_INTERVAL_S)


def _devin_pane_error(pane: str) -> str:
    """Return a short pane-visible failure reason, or empty string."""
    lowered = pane.lower()
    for needle, message in (
        ("not logged in", "Devin is not logged in — run `devin auth login`."),
        ("authentication", "Devin reported an authentication problem."),
        ("command not found", "The `devin` binary was not found in the terminal."),
        ("workspace trust", "Devin is waiting on a workspace-trust decision."),
    ):
        if needle in lowered:
            return message
    return ""


def _wait_for_devin_input_ready(
    socket_path: str,
    tmux_target: str,
    *,
    timeout_s: float,
) -> None:
    """Block until Devin's composer renders, tolerating a slow cold boot."""
    deadline = time.monotonic() + timeout_s
    boot_deadline = time.monotonic() + max(timeout_s, _DEVIN_BOOT_READY_TIMEOUT_S)
    while True:
        pane = _capture_pane(socket_path, tmux_target)
        if devin_input_ready(pane):
            return
        failure = _devin_pane_error(pane)
        if failure:
            raise RuntimeError(failure)
        now = time.monotonic()
        # While the boot banner is up Devin is provably still coming up, so keep
        # waiting to the longer ceiling instead of failing a healthy TUI.
        if now >= deadline and not (_devin_still_booting(pane) and now < boot_deadline):
            break
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(
        f"Devin's composer did not become ready within {timeout_s:.0f}s; "
        "the TUI may still be starting or is waiting on input."
    )


def _submit_needle(content: str) -> str:
    """Return a small marker used to identify the pasted draft."""
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        for idx, ch in enumerate(line):
            if ord(ch) < 0x20:
                line = line[:idx]
                break
        line = line.strip()
        if line:
            return line[:24]
    return ""


def _draft_in_input_region(pane: str, needle: str, baseline_region: str) -> bool:
    """Return whether the pasted draft is still visible in the composer."""
    region = _devin_input_region(pane)
    if not needle or region == baseline_region:
        return False
    normalized = needle.strip()
    if not normalized:
        return False
    for raw in region.splitlines():
        # Strip Devin's composer prompt glyph before comparing.
        line = raw.strip().lstrip("❭❯>").strip()
        if line == normalized or line.startswith(normalized):
            return True
    return False


def _paste_payload_bytes(text: str) -> bytes:
    r"""Encode text for ``tmux load-buffer``.

    Line breaks become CR, tabs are kept, and other control bytes are dropped —
    a stray ESC would terminate the bracketed paste early and submit a partial
    message.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    body = bytearray()
    for ch in normalized:
        if ch == "\n":
            body.append(0x0D)
            continue
        if ch == "\t":
            body.append(0x09)
            continue
        if ord(ch) < 0x20:
            continue
        body.extend(ch.encode("utf-8"))
    return bytes(body)


def _paste_literal_text(socket_path: str, tmux_target: str, bridge_dir: Path, text: str) -> None:
    """Deliver text into Devin via a tmux bracketed paste (multi-line safe).

    ``send-keys -l`` sends interior newlines as raw Enter keys, so a multi-line
    web message would submit line-by-line on the first break. ``load-buffer`` +
    ``paste-buffer -p`` wraps the text in bracketed-paste markers so Devin's
    composer keeps the line breaks as draft data. The trailing newline absorbs
    any trailing backslash so it cannot escape the follow-up Enter.
    """
    with tempfile.NamedTemporaryFile(
        dir=bridge_dir, prefix="paste_", suffix=".bin", delete=False
    ) as paste_file:
        paste_file.write(_paste_payload_bytes(text + "\n"))
        paste_path = paste_file.name
    try:
        _run_tmux(socket_path, "load-buffer", "-b", _PASTE_BUFFER, paste_path)
        _run_tmux(
            socket_path,
            "paste-buffer",
            "-p",  # bracketed-paste markers — the TUI keeps newlines as data
            "-d",  # drop the buffer after pasting
            "-b",
            _PASTE_BUFFER,
            "-t",
            tmux_target,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(paste_path)


def inject_user_message(
    bridge_dir: Path,
    *,
    content: str,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """Deliver a web-UI user message into the Devin TUI composer.

    :param bridge_dir: Per-session bridge directory.
    :param content: Message text (may be multi-line).
    :param timeout_s: Budget for tmux/composer readiness.
    :raises RuntimeError: If the pane is gone or Devin never accepts the draft.
    """
    if not content:
        raise RuntimeError("devin-native injection requires non-empty content")
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    _wait_for_forwarder_ready_if_required(
        bridge_dir,
        tmux_info=_read_bridge_json(bridge_dir, _TMUX_FILE) or {},
        timeout_s=timeout_s,
    )
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    if not _session_alive(socket_path, tmux_target):
        raise RuntimeError(
            "the Devin terminal is no longer running (the TUI exited); restart the session"
        )
    _wait_for_devin_input_ready(socket_path, tmux_target, timeout_s=timeout_s)
    # Clear any stale draft the user left in the composer.
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-a")
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-k")
    baseline_region = _devin_input_region(_capture_pane(socket_path, tmux_target))
    _paste_literal_text(socket_path, tmux_target, bridge_dir, content)
    needle = _submit_needle(content)
    draft_seen = False
    if needle:
        deadline = time.monotonic() + _TYPE_COMMIT_TIMEOUT_S
        while time.monotonic() < deadline:
            if _draft_in_input_region(
                _capture_pane(socket_path, tmux_target), needle, baseline_region
            ):
                draft_seen = True
                break
            time.sleep(_POLL_INTERVAL_S)
    time.sleep(_TYPE_SETTLE_S)
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")
    if not draft_seen:
        # Never observed the draft, so there is nothing to verify against —
        # the Enter above is the best-effort submit.
        _flush_devin_queue(socket_path, tmux_target)
        return
    deadline = time.monotonic() + _SUBMIT_VERIFY_TIMEOUT_S
    last_enter = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_S)
        if not _draft_in_input_region(
            _capture_pane(socket_path, tmux_target), needle, baseline_region
        ):
            _flush_devin_queue(socket_path, tmux_target)
            return
        if time.monotonic() - last_enter >= _SUBMIT_RETRY_INTERVAL_S:
            _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")
            last_enter = time.monotonic()
    raise RuntimeError("Devin did not accept the submitted message; the draft is still visible")


def inject_slash_command(
    bridge_dir: Path,
    *,
    command: str,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """Send a Devin slash command (e.g. ``/model opus``) into the TUI.

    Slash commands are single-line, so they go in with ``send-keys -l`` rather
    than a bracketed paste — Devin's command palette filters as you type and a
    paste can race the popup.

    :param bridge_dir: Per-session bridge directory.
    :param command: Slash command including the leading ``/``.
    :raises RuntimeError: If *command* does not start with ``/`` or carries a
        control byte.
    """
    if not command.startswith("/"):
        raise RuntimeError(f"devin-native slash command must start with '/': {command!r}")
    # `send-keys -l` types the string literally, so a CR/ESC would land as real
    # keystrokes able to submit a second command. The paste path already drops
    # control bytes (`_paste_payload_bytes`); refuse them here rather than strip,
    # since every caller builds the command from a validated catalog id.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in command):
        raise RuntimeError(f"devin-native slash command must not carry control bytes: {command!r}")
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    _wait_for_devin_input_ready(socket_path, tmux_target, timeout_s=timeout_s)
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-a")
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "C-k")
    _run_tmux(socket_path, "send-keys", "-l", "-t", tmux_target, command)
    time.sleep(_TYPE_SETTLE_S)
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Enter")


def inject_model_command(
    bridge_dir: Path,
    *,
    model: str,
    timeout_s: float = _TMUX_READY_TIMEOUT_S,
) -> None:
    """Switch the live Devin session onto *model* via ``/model <name>``."""
    inject_slash_command(bridge_dir, command=f"/model {model}", timeout_s=timeout_s)


def inject_interrupt(bridge_dir: Path, *, timeout_s: float = _TMUX_READY_TIMEOUT_S) -> None:
    """Cancel the in-flight Devin turn.

    Devin's own hint reads "esc twice to interrupt": a single Escape only
    clears the composer draft, so one key leaves the turn running. Sending two,
    spaced apart, is what actually aborts it — verified against devin
    3000.10.21, which then shows "✱ Canceled." and returns the composer to its
    idle placeholder.

    The harness ``run_turn`` returns right after the paste, so the runner's
    in-process cancel floor cannot reach the turn; this is the web UI's Stop
    button.

    :raises RuntimeError: If the tmux target is not advertised or send-keys fails.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    socket_path = info["socket_path"]
    tmux_target = info["tmux_target"]
    # No ``-l``: tmux must interpret ``Escape`` as a key name.
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Escape")
    time.sleep(_INTERRUPT_KEY_INTERVAL_S)
    _run_tmux(socket_path, "send-keys", "-t", tmux_target, "Escape")


def kill_session(bridge_dir: Path, *, timeout_s: float = _TMUX_READY_TIMEOUT_S) -> None:
    """Hard-stop the Devin session by killing its tmux session.

    Terminates ``devin`` and the pane outright — the analog of the user
    manually exiting the attached TUI, for the web UI's "Stop session"
    affordance.
    """
    info = _wait_for_tmux_info(bridge_dir, timeout_s=timeout_s)
    try:
        _run_tmux(info["socket_path"], "kill-session", "-t", info["tmux_target"])
    except RuntimeError as exc:
        # Already gone is success for a stop button.
        if "can't find session" not in str(exc) and "no server running" not in str(exc):
            raise


#: Flags Omnigent itself puts in Devin's argv. A passthrough copy hijacks the
#: session (``--resume``), defeats the policy gate (``--config``), redirects the
#: transcript (``--export``) or re-arms the trust prompt — and Devin refuses a
#: repeated flag outright, so the launch would otherwise fail cryptically. This is
#: the funnel every ingress reaches (CLI args, ``harness.devin-native.args`` from
#: config, and API-persisted ``terminal_launch_args``), which the CLI-only guard
#: in ``cli.py`` never saw.
#:
#: ``--permission-mode`` is deliberately NOT reserved: the web create flow
#: delivers the user's picked mode through these very args.
_RESERVED_PASSTHROUGH_FLAGS = frozenset(
    {"--config", "--export", "--resume", "-r", "--continue", "-c", "--respect-workspace-trust"}
)


def _reject_reserved_passthrough(passthrough: Sequence[str]) -> None:
    """Refuse launch args that would override Omnigent's own Devin flags.

    :param passthrough: Extra args destined for Devin's argv.
    :raises RuntimeError: If any names an Omnigent-owned flag.
    """
    named = {arg.split("=", 1)[0] for arg in passthrough}
    reserved = sorted(named & _RESERVED_PASSTHROUGH_FLAGS)
    if reserved:
        raise RuntimeError(
            "devin-native launch args may not override Omnigent-owned flags: "
            f"{', '.join(reserved)}"
        )


def build_devin_launch_args(
    passthrough: Sequence[str],
    *,
    config_path: Path,
    export_path_value: Path,
    model: str | None = None,
    permission_mode: str | None = None,
    resume_id: str | None = None,
    sandbox: bool = False,
) -> list[str]:
    """Build the ``devin`` argv tail (everything after the executable).

    ``--export`` is always passed: Devin rewrites the ATIF transcript after
    every turn, which is where the forwarder reads reasoning text and token
    metrics that the hook payloads do not carry.

    :param passthrough: Extra user-supplied args appended last.
    :param config_path: Session-scoped config (carries the Omnigent hooks).
    :param export_path_value: Where Devin writes the ATIF transcript.
    :param model: Devin model id (family slug or full variant).
    :param permission_mode: One of Devin's permission modes.
    :param resume_id: Devin session id to resume.
    :param sandbox: Whether to enable Devin's OS-level sandbox.
    """
    _reject_reserved_passthrough(passthrough)
    args = ["--config", str(config_path), "--export", str(export_path_value)]
    # Omnigent owns workspace trust: the runner only launches in a workspace the
    # user already chose, and an un-dismissable trust prompt would wedge the pane.
    args.extend(["--respect-workspace-trust", "false"])
    if resume_id:
        args.extend(["--resume", resume_id])
    if model:
        args.extend(["--model", model])
    if permission_mode:
        args.extend(["--permission-mode", permission_mode])
    if sandbox:
        args.append("--sandbox")
    args.extend(passthrough)
    return args
