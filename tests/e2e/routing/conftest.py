"""Fixtures for the e2e Smart Routing CUJ suite.

The stack each CUJ runs against:

* :func:`mock_router` — the deterministic ``routes:select`` service
  (``_mock_router``). No AI-Gateway ``task_v1`` dependency, so the routing
  verdicts are fixed and the assertions can name exact arms and rationales.
* :func:`routing_server` — a real ``omnigent server`` subprocess on a free
  port with a temp sqlite DB, whose ``routing:`` block points at the mock.
  Modeled on ``tests/e2e/conftest.py``'s ``live_server``: health poll before
  yielding, log tail on timeout, SIGTERM teardown.
* :func:`routing_host` — a real ``omnigent host`` daemon registered against
  that server, under the developer's real ``$HOME`` (the ``claude`` / ``codex``
  logins cannot be relocated) but an isolated ``OMNIGENT_CONFIG_HOME`` /
  ``OMNIGENT_DATA_DIR``. It never reads or writes ``~/.omnigent``.

The panes launched by these tests do real inference on the gateway, so no CUJ
asserts on answer content — only on routing artifacts. That is deliberate: a
slow or failed generation must not be able to redden a routing test.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.routing._helpers import POLL_INTERVAL_S, wait_for
from tests.e2e.routing._mock_router import MockRouter, serve_mock_router

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Opt-in gate. This suite launches real ``claude`` / ``codex`` TUIs against a
#: real gateway, so it never runs in CI by accident.
_RUN_GATE_ENV = "OMNIGENT_E2E_SMART_ROUTING"

#: Second gate for the repeat-run reliability variants (CUJ 4's 5x nightly
#: form), which multiply an already slow suite.
RELIABILITY_GATE_ENV = "OMNIGENT_E2E_RELIABILITY"

#: Where the provider config for the host comes from. The routing worktree's
#: isolated dev config is the default; override to point at another workspace.
_PROVIDER_CONFIG_ENV = "OMNIGENT_E2E_ROUTING_PROVIDER_CONFIG"

#: Seconds to wait for the server's health endpoint.
_SERVER_HEALTH_TIMEOUT_S = 60.0

#: Seconds to wait for the host daemon to register.
_HOST_REGISTER_TIMEOUT_S = 60.0

#: Catalog prefixes this deployment's model ids carry and the router does not
#: expect. ``system.ai.`` keeps its trailing dot — without it ids strip to
#: ``.claude-opus-5`` and malformed names reach the router.
MODEL_PREFIXES: tuple[str, ...] = ("databricks-", "system.ai.")

#: Where :func:`routing_server` records its log path, so
#: :func:`routing_server_log` can hand it to a test without guessing at the
#: temp-dir naming the factory chose.
_server_log_path: list[Path] = []


@pytest.fixture(autouse=True, scope="session")
def _require_opt_in() -> None:
    """Skip the whole suite unless it was asked for explicitly."""
    if os.environ.get(_RUN_GATE_ENV) != "1":
        pytest.skip(
            f"set {_RUN_GATE_ENV}=1 to run the Smart Routing e2e CUJs "
            "(they launch real claude/codex TUIs and a real host daemon)"
        )


def _free_port() -> int:
    """Return a free TCP port on loopback.

    :returns: A port number nothing is currently listening on.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def mock_router() -> Iterator[MockRouter]:
    """Run the deterministic ``routes:select`` mock for the session.

    :yields: The router handle; ``base_url`` goes into the server's
        ``routing.base_url``.
    """
    yield from serve_mock_router()


@pytest.fixture(autouse=True)
def _reset_mock_router(mock_router: MockRouter) -> Iterator[None]:
    """Clear the mock's served-request log between tests.

    Several CUJs count routing calls ("turn 2 must issue zero"), which only
    means anything against a per-test baseline.
    """
    mock_router.reset()
    yield


def _provider_block() -> dict[str, Any]:
    """Read the ``providers:`` block the host needs for gateway-backed launches.

    Smart Routing's apply layer can only rewrite a launch's model when the
    launch resolves through the Databricks AI Gateway, so the host has to see a
    databricks provider. The routing worktree's isolated dev config is the
    canonical source (three canonical prompt classes: trivial, delegate-shaped, crosscutting).

    :returns: The ``providers`` mapping.
    :raises pytest.skip.Exception: When no provider config can be found.
    """
    override = os.environ.get(_PROVIDER_CONFIG_ENV)
    candidates = [Path(override)] if override else [_REPO_ROOT / ".omnigent-local" / "config.yaml"]
    for path in candidates:
        if not path.is_file():
            continue
        parsed = yaml.safe_load(path.read_text()) or {}
        providers = parsed.get("providers")
        if isinstance(providers, dict) and providers:
            return providers
    pytest.skip(
        "no gateway provider config found "
        f"(looked at {', '.join(str(c) for c in candidates)}); "
        f"set {_PROVIDER_CONFIG_ENV} to a config.yaml carrying a databricks provider"
    )


@pytest.fixture(scope="session")
def routing_server(
    mock_router: MockRouter,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Start an ``omnigent server`` whose router is the mock; yield its base URL.

    A ``live_server`` variant: free port, temp sqlite, health poll before
    yielding, server log tailed into the failure message, SIGTERM teardown. The
    only routing source configured is ``external`` pointed at the mock — there
    is deliberately no server ``llm:`` block, so the built-in judge is
    unavailable and every decision must come from the external client. That is
    what makes ``router_source == "databricks-aigw"`` a real assertion rather
    than a coin flip.

    :param mock_router: The mock whose base URL becomes ``routing.base_url``.
    :param tmp_path_factory: Pytest temp path factory.
    :yields: The server base URL, e.g. ``"http://127.0.0.1:54321"``.
    """
    port = _free_port()
    root = tmp_path_factory.mktemp("routing_e2e_server")
    db_path = root / "routing_e2e.db"
    config_path = root / "server.yaml"
    log_path = root / "server.log"
    _server_log_path.append(log_path)
    config_path.write_text(
        yaml.safe_dump(
            {
                "routing": {
                    "provider": "external",
                    "base_url": mock_router.base_url,
                    "router_name": "task_v1",
                    "model_prefix": list(MODEL_PREFIXES),
                }
            },
            sort_keys=False,
        )
    )

    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT), "OMNIGENT_LOG_TO_STDERR": "1"}
    log_handle = open(log_path, "w")  # noqa: SIM115 — lives for the subprocess's lifetime
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(root / "artifacts"),
            "--config",
            str(config_path),
        ],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"

    def _tail() -> str:
        return log_path.read_text(errors="replace")[-4000:] if log_path.exists() else ""

    deadline = time.monotonic() + _SERVER_HEALTH_TIMEOUT_S
    healthy = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                healthy = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(POLL_INTERVAL_S)
    if not healthy:
        proc.kill()
        proc.wait(timeout=5)
        log_handle.close()
        raise RuntimeError(
            f"routing server did not start within {_SERVER_HEALTH_TIMEOUT_S}s.\n"
            f"log at {log_path}:\n{_tail()}"
        )

    try:
        yield base_url
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_handle.close()


@pytest.fixture(scope="session")
def routing_server_log(routing_server: str) -> Path:
    """Return the routing server's log path.

    Recipe R4 reads it for the routing gate lines (``route_turn skipped …``,
    ``harness=X cannot run Y``, ``routes:select returned …``).

    :param routing_server: Forces the server to be up, which is what records
        the path.
    :returns: The ``server.log`` path.
    """
    del routing_server
    assert _server_log_path, "the routing server fixture did not record its log path"
    return _server_log_path[-1]


@pytest.fixture(scope="session")
def routing_client(routing_server: str) -> Iterator[httpx.Client]:
    """HTTP client pointed at the routing server.

    :param routing_server: The server base URL.
    :yields: A client announcing itself as a first-party non-browser caller.
    """
    with httpx.Client(
        base_url=routing_server,
        timeout=120,
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    ) as client:
        yield client


@pytest.fixture(scope="session")
def routing_host(
    routing_server: str,
    routing_client: httpx.Client,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Spawn an ``omnigent host`` against the routing server; yield its host id.

    The daemon inherits the real ``$HOME`` — the ``claude`` and ``codex``
    logins live there and cannot be relocated — but its omnigent config home
    and data dir are temporary, seeded with only the gateway ``providers:``
    block. ``~/.omnigent/config.yaml`` is never read or written.

    :param routing_server: Server URL the daemon registers with.
    :param routing_client: Used to poll ``GET /v1/hosts``.
    :param tmp_path_factory: Pytest temp path factory.
    :yields: The registered host's ``host_id``.
    """
    providers = _provider_block()
    root = tmp_path_factory.mktemp("routing_e2e_host")
    config_home = root / "config-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        yaml.safe_dump({"providers": providers}, sort_keys=False)
    )
    log_path = root / "host.log"

    env = {
        **os.environ,
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_DATA_DIR": str(root / "data"),
        "PYTHONPATH": str(_REPO_ROOT),
        "OMNIGENT_LOG_TO_STDERR": "1",
    }
    # Claude Code refuses to start a nested session; the agent process running
    # this suite may export the marker.
    env.pop("CLAUDECODE", None)
    log_handle = open(log_path, "w")  # noqa: SIM115 — lives for the subprocess's lifetime
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", routing_server],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=log_handle,
    )

    def _online() -> str | None:
        if proc.poll() is not None:
            return None
        resp = routing_client.get("/v1/hosts")
        if resp.status_code != 200:
            return None
        online = [h for h in resp.json().get("hosts", []) if h.get("status") == "online"]
        return str(online[0]["host_id"]) if online else None

    try:
        host_id = wait_for(
            _online,
            timeout=_HOST_REGISTER_TIMEOUT_S,
            what="the host daemon to register",
        )
    except AssertionError as exc:
        proc.kill()
        proc.wait(timeout=5)
        log_handle.close()
        tail = log_path.read_text(errors="replace")[-3000:] if log_path.exists() else ""
        pytest.skip(f"could not bring up an omnigent host: {exc}\nhost log tail:\n{tail}")

    try:
        yield host_id
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_handle.close()


@pytest.fixture
def routing_arms(
    routing_client: httpx.Client,
    routing_host: str,
) -> Callable[..., None]:
    """Return a per-test guard on the arms a CUJ needs being gateway-backed.

    A family whose inference is not AI-Gateway-backed cannot run the router's
    picks, and the server correctly hides Smart Routing for it — so a CUJ over
    that family would be scoring the gate, not the routing. Unknown is not
    "off the gateway": a host reporting nothing for a family keeps the gateway
    router, so only an explicit ``false`` skips.

    :param routing_client: Used to read ``GET /v1/hosts``.
    :param routing_host: The host whose readiness is read.
    :returns: ``require(*harnesses)``, which skips with a named reason.
    """

    def _require(*harnesses: str) -> None:
        resp = routing_client.get("/v1/hosts")
        resp.raise_for_status()
        row = next(
            (h for h in resp.json().get("hosts", []) if h.get("host_id") == routing_host),
            None,
        )
        assert row is not None, f"host {routing_host} vanished from /v1/hosts"
        backing = row.get("gateway_inference") or {}
        unbacked = [h for h in harnesses if backing.get(h) is False]
        if unbacked:
            pytest.skip(
                f"not AI-Gateway-backed on this host: {', '.join(unbacked)} — "
                "Smart Routing is correctly hidden for those families, so this CUJ "
                "would score the gate instead of the routing"
            )
        for harness in harnesses:
            cli = {"claude-native": "claude", "codex-native": "codex"}.get(harness)
            if cli is not None and shutil.which(cli) is None:
                pytest.skip(f"{cli!r} CLI is not on PATH; {harness} cannot launch")

    return _require


@pytest.fixture
def type_into_tui() -> Callable[[str, str, str], None]:
    """Return a helper that TYPES text into a tmux pane and presses Enter.

    The TUI halves of this suite must type, not POST: a posted message enters
    through the server's composer path, which routes on a different gate
    entirely. Only a keystroke reaches the harness's ``UserPromptSubmit`` hook,
    which is what the first-message CUJs are about.

    ``load-buffer`` + ``paste-buffer -p`` delivers the text as one bracketed
    paste, so a multi-line prompt cannot be interpreted line-by-line as
    several submissions; Enter is sent separately once the paste has landed.

    :returns: ``type_into(socket_path, target, text)``.
    """

    def _type_into(socket_path: str, target: str, text: str) -> None:
        subprocess.run(
            ["tmux", "-S", socket_path, "load-buffer", "-b", "omni-routing-e2e", "-"],
            input=text.encode("utf-8"),
            check=True,
            timeout=15,
        )
        subprocess.run(
            [
                "tmux",
                "-S",
                socket_path,
                "paste-buffer",
                "-p",
                "-b",
                "omni-routing-e2e",
                "-t",
                target,
            ],
            check=True,
            timeout=15,
        )
        # Let the TUI's input box absorb the paste before submitting it.
        time.sleep(1.0)
        subprocess.run(
            ["tmux", "-S", socket_path, "send-keys", "-t", target, "Enter"],
            check=True,
            timeout=15,
        )

    return _type_into


def create_routed_session(
    client: httpx.Client,
    *,
    agent_name: str,
    host_id: str,
    workspace: Path,
    message: str | None = None,
    harness_override: str | None = None,
    terminal_launch_args: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Create a Smart Routing session the way the web UI's create does.

    The routing contract is ``cost_control_mode_override="on"`` plus **no**
    model or effort pin — a pin silently disables routing. A
    ``smart_routing_message`` routes at create time; omitting it leaves the
    pick to the harness's own first-message hook.

    :param client: HTTP client pointed at the routing server.
    :param agent_name: Built-in wrapper agent, e.g. ``"claude-native-ui"``.
    :param host_id: Host to launch on.
    :param workspace: Absolute workspace path on that host.
    :param message: The first-message text to route on, or ``None`` for a bare
        Smart Routing create.
    :param harness_override: ``"auto"`` to let the router pick the harness too.
    :param terminal_launch_args: Pass-through CLI args for the native launch,
        e.g. :func:`bypass_args`.
    :returns: The create response body.
    """
    agents = client.get("/v1/agents")
    agents.raise_for_status()
    agent_id = next(
        (a["id"] for a in agents.json()["data"] if a["name"] == agent_name),
        None,
    )
    assert agent_id is not None, f"{agent_name!r} is not registered on the server"
    body: dict[str, Any] = {
        "agent_id": agent_id,
        "host_id": host_id,
        "workspace": str(workspace),
        "cost_control_mode_override": "on",
    }
    if message is not None:
        body["smart_routing_message"] = message
    if harness_override is not None:
        body["harness_override"] = harness_override
    if terminal_launch_args:
        body["terminal_launch_args"] = list(terminal_launch_args)
    resp = client.post("/v1/sessions", json=body, timeout=120.0)
    assert resp.status_code < 400, f"routed create failed {resp.status_code}: {resp.text[:2000]}"
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


def post_user_message(client: httpx.Client, session_id: str, text: str) -> None:
    """POST *text* into *session_id* as a user message event.

    :param client: HTTP client pointed at the routing server.
    :param session_id: Session id.
    :param text: The raw message text; never wrapped, because ``task.prompt``
        is the whole routing signal.
    """
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
        timeout=60.0,
    )
    assert resp.status_code < 400, f"message POST failed {resp.status_code}: {resp.text[:1000]}"


def trusted_workspace(root: Path, name: str) -> Path:
    """Create a workspace dir the native CLIs will start in without prompting.

    :param root: Parent temp dir.
    :param name: Subdirectory name.
    :returns: The created workspace path.
    """
    workspace = root / name
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "README.md").write_text("# routing e2e workspace\n")
    return workspace


def prompts() -> dict[str, str]:
    """Return the canonical CUJ prompts, keyed by handle.

    Held verbatim: the prompt IS the whole routing signal, so any wrapper or
    paraphrase changes the answer.

    :returns: ``{"trivial": ..., "delegate": ..., "crosscutting": ...}``.
    """
    return {
        "trivial": "hi",
        "delegate": (
            "The `parse_duration` helper in our config module returns None for values "
            'like "1h30m" because its regex only matches a single unit group, so any '
            "compound duration silently becomes None and the caller falls back to the "
            "default timeout. Fix the parser to accept compound durations combining "
            'days, hours, minutes, and seconds (e.g. "1h30m", "2d4h", "45s") and '
            "return the total number of seconds as an int."
        ),
        "escalate": (
            "Add a --dry-run flag to our `deploy` CLI command. When passed, the command "
            "should resolve the full deployment plan and print it as a human-readable "
            "table, then exit 0 without calling the orchestration API or mutating any "
            "state. Reuse the existing plan-resolution logic from the real run."
        ),
        "crosscutting": (
            "We need to rethink routing across every surface end to end: the server, "
            "the runner, both native harnesses, the CLI entry points and the web chips. "
            "There are many open questions and the migration touches everything, so "
            "start by mapping what exists today and where the seams are, then propose "
            "an ordering that keeps each step shippable on its own. "
        )
        * 4,
    }


def unique_trivial_prompt(label: str) -> str:
    """Return a rule-0 prompt no other test in the session can collide with.

    Rule-0 keys on length and the absence of error/code markers, so a unique
    tail keeps the routing verdict identical while making the prompt findable in
    the mock's served-request log — which is how the "exactly one routing call"
    and "zero routing calls" assertions stay immune to a pane still working in
    another CUJ.

    :param label: Short marker naming the caller, e.g. ``"claude-turn-1"``.
    :returns: A short, code-free prompt.
    """
    return f"say hello and nothing else, run marker {label} {uuid.uuid4().hex[:8]}"


def spawn_prompt(*, task: str, tool_hint: str) -> str:
    """Build a message that reliably makes a coding CLI issue one spawn.

    Whether a model *chooses* to delegate is judgement; this suite is about how
    a spawn is ROUTED, so the instruction is explicit. The delegated task text
    is what the router scores, so it is passed through verbatim.

    :param task: The sub-task text to hand the subagent.
    :param tool_hint: The harness's own spawn-tool name, e.g. ``"Task"``.
    :returns: The message to send.
    """
    return (
        f"Use your {tool_hint} tool right now to start exactly one subagent, and do "
        "no other work first. Give the subagent exactly this task, verbatim:\n\n"
        f"{task}\n\n"
        "Do not do the task yourself and do not wait for the subagent's answer — "
        "issue the spawn, then reply with the single word DISPATCHED."
    )


def bypass_args(harness: str) -> list[str]:
    """Launch args that get a native CLI past its first-run gates.

    A fresh temp workspace is untrusted, and both CLIs block their input box on
    a trust / approval dialog before any hook can fire — which would make these
    CUJs time out on a dialog rather than score routing. Passing the bypass flag
    through ``terminal_launch_args`` keeps the developer's ``$HOME`` untouched;
    the alternative (seeding trust entries into ``~/.claude.json``) mutates it.

    :param harness: ``"claude-native"`` or ``"codex-native"``.
    :returns: Args for the create's ``terminal_launch_args``.
    """
    if harness == "claude-native":
        return ["--dangerously-skip-permissions"]
    return ["--dangerously-bypass-approvals-and-sandbox"]


def spawn_tool_for(harness: str) -> str:
    """Return the built-in spawn tool a harness advertises.

    :param harness: ``"claude-native"`` or ``"codex-native"``.
    :returns: The tool name to name in a prompt.
    """
    return "Task" if harness == "claude-native" else "spawn_agent"


def require_tmux() -> None:
    """Skip when ``tmux`` is missing — the TUI halves cannot be driven without it."""
    if shutil.which("tmux") is None:
        pytest.skip("'tmux' is not on PATH; the TUI halves of this suite need it")


def sequence(values: Sequence[str]) -> str:
    """Join *values* for a readable assertion message.

    :param values: Strings to join.
    :returns: A comma-separated list, or ``"<none>"`` when empty.
    """
    return ", ".join(values) if values else "<none>"
