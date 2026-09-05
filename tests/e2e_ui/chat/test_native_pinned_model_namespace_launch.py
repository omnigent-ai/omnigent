"""E2E: a gateway-namespace model pin must not abort a native terminal launch.

Regression test for the cross-namespace launch-gate rejection: native
claude/codex terminal launches fail hard when the model id
the session is pinned to is spelled in a different namespace than the host's
*launchable* catalog rows, even though the pin denotes a model that catalog
serves. The pre-launch gate (``catalog_contains`` in
``omnigent/model_catalog_store.py``) does a bare exact string compare with no
folding of the mechanical ``databricks-`` / ``system.ai.`` prefixes or of
codex's dotted-vs-dashed version spelling, so:

* claude: a pin like ``system.ai.claude-opus-4-8[1m]`` is rejected while the
  launch catalog lists ``('opus[1m]', 'claude-opus-4-8[1m]')``;
* codex: a pin like ``databricks-gpt-5-5`` is rejected while the launch
  catalog lists ``('gpt-5.5', 'gpt-5.5')``.

The launch aborts with ``the requested model ... is not in this host's
current model list`` *before* the CLI process starts, the session goes
``failed``, and the user cannot recover from inside the product (the pin can
live in a deployed agent spec, so a provider-side retirement bricks every
session of that agent at once).

The journey this drives is the reported one: create a native-terminal
session, pin its model in the gateway/catalog spelling (what a deployed spec
pin or an orchestrator pick carries), open the session so the runner launches
the terminal, and watch the launch outcome. To stay truthful across CLI
upgrades the pin is *derived* from the host's own launchable catalog (probed
through the production ``codex_launch_catalog`` / ``claude_launch_catalog``
paths under the rig's isolated HOME), then re-spelled into the gateway
namespace — so by construction the pinned id denotes a model this host can
launch, only spelled differently.

While the bug is live the launch gate aborts and this test FAILS with the
gate's own message; after a fix that folds equivalent spellings (or degrades
within the family), the terminal comes up and the test passes.

The rig mirrors ``test_codex_native_headless_login_timeout.py``: a dedicated
server + runner pair with isolated ``HOME`` / ``OMNIGENT_CONFIG_HOME`` so no
ambient provider config or credential leaks in. No LLM traffic is needed —
the failure under test happens before any CLI process starts, and on the
fixed path the terminal resource registers even without a usable login.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Boot budget for the spawned server + runner pair.
_HEALTH_TIMEOUT_S = 90.0
# The launch-catalog probe boots the real CLI (codex app-server / claude -p);
# cold first runs on a loaded CI box need room.
_PROBE_TIMEOUT_S = 300.0
# Launch outcome budget: terminal auto-create includes its own catalog read
# (warmed by the probe above) plus the tmux/CLI boot on the fixed path.
_OUTCOME_TIMEOUT_S = 240.0

# The launch gate's silent-fallback warnings (runner/native/orchestration.py):
# a pin the gate cannot place on the catalog launches on the provider default
# and logs one of these instead of honoring the pick.
_FALLBACK_MARKERS = {
    "claude": "claude-native: model pick",
    "codex": "codex-native: model pick",
}

_ERROR_PILL = '[data-testid="error-pill"]'

# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars
# that must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


#: Ambient provider/credential variables that would otherwise leak into the
#: rig's provider resolution and catalog probe, making the journey depend on
#: the developer's or CI box's own configuration rather than the isolated
#: HOME / OMNIGENT_CONFIG_HOME the rig sets up.
_AMBIENT_PROVIDER_ENV_PREFIXES = (
    "OPENAI_",
    "ANTHROPIC_",
    "CLAUDE_CODE_",
    "DATABRICKS_",
    "CODEX_",
)


def _no_proxy_env() -> dict[str, str]:
    """Ambient env with loopback proxy-exempt and provider config stripped."""
    env = os.environ.copy()
    for var in list(env):
        if var.startswith(_AMBIENT_PROVIDER_ENV_PREFIXES):
            env.pop(var)
    for var in ("NO_PROXY", "no_proxy"):
        existing = env.get(var, "")
        env[var] = ",".join(filter(None, [existing, "127.0.0.1,localhost"]))
    return env


@dataclass
class _NativeRig:
    """A dedicated server + runner pair with an isolated home/config."""

    base_url: str
    runner_id: str
    env: dict[str, str]
    work: Path
    server_log: Path
    runner_log: Path


@pytest.fixture
def native_launch_rig(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[_NativeRig]:
    """Spawn an isolated server + runner for the pinned-model launch journey.

    Isolated ``HOME`` / ``OMNIGENT_CONFIG_HOME`` keep ambient provider
    config, CLI logins, and the developer's shared model-catalog store out
    of the rig, so the launch gate under test reads exactly the catalog the
    rig's own probe writes.

    :returns: The rig handle (base URL, runner id, subprocess env).
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("pinned-model launch e2e requires an isolated spawned server")

    work = tmp_path_factory.mktemp("native_pin_launch")
    config_home = work / "config-home"
    home_dir = work / "home"
    state_dir = work / "codex-native-state"
    artifacts = work / "artifacts"
    for path in (config_home, home_dir, state_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)

    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    # sdks/ paths cover source-tree runs where the client/ui SDK packages are
    # importable from the repo rather than installed; harmless otherwise.
    pythonpath = os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            os.environ.get("PYTHONPATH", ""),
        ]
    )
    shared_env = {
        **_no_proxy_env(),
        "PYTHONPATH": pythonpath,
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "HOME": str(home_dir),
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    server_log = work / "server.log"
    runner_log = work / "runner.log"
    server_handle = server_log.open("w")
    runner_handle = runner_log.open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{work}/test.db",
                "--artifact-location",
                str(artifacts),
            ],
            env=server_env,
            stdout=server_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            if server_proc.poll() is not None or runner_proc.poll() is not None:
                break
            try:
                if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online"):
                        online = True
                        break
            except httpx.HTTPError:
                # The rig is still booting; retry until the deadline.
                time.sleep(0.5)
                continue
            time.sleep(0.5)
        if not online:
            raise RuntimeError(
                "pinned-model launch rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )

        yield _NativeRig(
            base_url=base_url,
            runner_id=runner_id,
            env=shared_env,
            work=work,
            server_log=server_log,
            runner_log=runner_log,
        )
    finally:
        for proc in (runner_proc, server_proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in (runner_proc, server_proc):
            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        server_handle.close()
        runner_handle.close()


def _probe_launch_catalog(harness: str, rig: _NativeRig) -> list[dict[str, object]]:
    """Probe the rig host's launchable catalog through the production path.

    Runs :func:`codex_launch_catalog` / :func:`claude_launch_catalog` in a
    subprocess carrying the rig's isolated env, so the probe writes the same
    shared catalog-store file the terminal launch will read (one probe, one
    catalog — no fingerprint drift).

    :param harness: ``"codex"`` or ``"claude"``.
    :param rig: The spawned rig (supplies HOME/config env).
    :returns: The catalog rows, e.g. ``[{"id": "gpt-5.5", "model": "gpt-5.5"}]``.
    """
    out_path = rig.work / f"{harness}-catalog.json"
    if harness == "codex":
        script = (
            "import asyncio, json, sys\n"
            "from omnigent.codex_native_app_server import codex_launch_catalog\n"
            "rows = asyncio.run(codex_launch_catalog())\n"
            "open(sys.argv[1], 'w').write(json.dumps(rows or []))\n"
        )
    else:
        script = (
            "import asyncio, json, sys\n"
            "from omnigent.claude_native import (\n"
            "    claude_launch_catalog,\n"
            "    resolve_native_claude_config,\n"
            ")\n"
            "cfg = resolve_native_claude_config(spec=None)\n"
            "rows = asyncio.run(claude_launch_catalog(cfg))\n"
            "open(sys.argv[1], 'w').write(json.dumps(rows or []))\n"
        )
    subprocess.run(
        [sys.executable, "-c", script, str(out_path)],
        env=rig.env,
        cwd=str(_REPO_ROOT),
        check=True,
        timeout=_PROBE_TIMEOUT_S,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    rows = json.loads(out_path.read_text())
    if not rows:
        pytest.skip(f"{harness} launch-catalog probe returned no rows on this host")
    return rows


def _gateway_spelling_of_served_model(
    harness: str, rows: list[dict[str, object]]
) -> tuple[str, str]:
    """Re-spell a served catalog model into the gateway/catalog namespace.

    This is the spelling a deployed agent-spec pin or an orchestrator pick
    carries for the SAME model the catalog row names (the report's
    ``system.ai.claude-opus-4-8[1m]`` / ``databricks-gpt-5-6-sol`` shapes).

    :param harness: ``"codex"`` or ``"claude"``.
    :param rows: The probed launchable catalog rows.
    :returns: ``(pin, served_spelling)``.
    """
    if harness == "codex":
        for row in rows:
            row_id = str(row.get("id") or row.get("model") or "")
            if row_id.startswith("gpt-"):
                # codex spells versions dotted; the gateway catalog dashes
                # them and prefixes the serving-endpoint namespace.
                return "databricks-" + row_id.replace(".", "-"), row_id
        pytest.skip("codex launch catalog lists no gpt-* model to re-spell")
    for row in rows:
        model = str(row.get("model") or "")
        if model.startswith("claude-"):
            return "system.ai." + model, model
    pytest.skip("claude launch catalog lists no claude-* model to re-spell")
    raise AssertionError("unreachable")


def _create_unbound_native_session(base_url: str, harness: str) -> str:
    """Register the native wrapper agent and create its session, unbound.

    Mirrors the conftest ``_create_native_codex_session`` /
    ``_create_native_claude_session`` factories (the exact terminal-first
    spec + wrapper labels the ``omnigent codex`` / ``omnigent claude`` CLIs
    ship) but does NOT bind the runner: the journey under test pins the
    session's model before the bind-triggered terminal auto-create runs,
    matching a session created against a spec/orchestrator pin.

    :param base_url: Spawned server base URL.
    :param harness: ``"codex"`` or ``"claude"``.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        CODEX_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )

    with tempfile.TemporaryDirectory() as tmp:
        if harness == "codex":
            from omnigent.codex_native import _materialize_codex_agent_spec

            spec_path = _materialize_codex_agent_spec(Path(tmp), model=None)
            wrapper_value = CODEX_NATIVE_WRAPPER_VALUE
        else:
            from omnigent.claude_native import _materialize_claude_agent_spec

            spec_path = _materialize_claude_agent_spec(Path(tmp))
            wrapper_value = CLAUDE_NATIVE_WRAPPER_VALUE
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname -> omnigent compat translator (the spec has
        # no spec_version), matching the conftest native session factories.
        info = tarfile.TarInfo(f"{harness}-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE, WRAPPER_LABEL_KEY: wrapper_value}
    # Codex terminals hard-require a workspace; the repo root matches the
    # conftest factories and is a valid dir on the runner's filesystem.
    metadata = {"labels": labels, "workspace": str(_REPO_ROOT)}
    create = _client.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": (f"{harness}-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _session_terminal_exists(base_url: str, session_id: str) -> bool:
    """Whether a terminal resource has registered for *session_id*."""
    resources = _client.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=5.0)
    if resources.status_code != 200:
        return False
    payload = resources.json()
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return False
    return any(isinstance(row, dict) and row.get("type") == "terminal" for row in rows)


def _session_launch_error(base_url: str, session_id: str) -> str | None:
    """The session's persisted launch-failure detail, if any.

    A launch abort publishes ``session.status: failed`` whose error the
    server persists as reload-visible labels projected into the snapshot's
    ``last_task_error`` — the durable, user-facing failure record.

    :returns: The failure message when the launch recorded one, else ``None``.
    """
    snapshot = _client.get(f"{base_url}/v1/sessions/{session_id}", timeout=5.0)
    if snapshot.status_code != 200:
        return None
    error = snapshot.json().get("last_task_error")
    if isinstance(error, dict):
        message = str(error.get("message") or "")
        if message:
            return message
    return None


@pytest.mark.timeout(900)
@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_gateway_namespace_pin_does_not_abort_native_launch(
    page: Page,
    native_launch_rig: _NativeRig,
    harness: str,
) -> None:
    """A pin naming a served model in gateway spelling launches on that model.

    Journey (the reported one): create a native codex/claude session, pin a
    model this host's launchable catalog serves — spelled in the gateway /
    catalog namespace, as a deployed agent spec or an orchestrator pick
    spells it — open the session, and let the runner launch the terminal.

    The original bug hard-failed this launch at the pre-launch model gate
    (session ``failed``, no terminal). The recovery contract that landed on
    main softened an unplaceable pin to a provider-default launch, so
    terminal registration alone no longer distinguishes a folded pin from a
    silently dropped one; the test therefore also asserts the gate did not
    log its not-served fallback for the pin. Opportunistic smoke: it skips
    without the real CLI on PATH — the autocreate unit tests assert the
    exact ``--model`` and pick survival deterministically.
    """
    if shutil.which(harness) is None:
        pytest.skip(f"{harness} CLI is required for the native pinned-model launch e2e")

    rig = native_launch_rig

    # The host's own launchable catalog, probed through the production
    # store path (also warms the store the launch gate will read).
    rows = _probe_launch_catalog(harness, rig)
    pin, served = _gateway_spelling_of_served_model(harness, rows)
    catalog_ids = [(row.get("id"), row.get("model")) for row in rows]
    assert not any(pin in spelling_pair for spelling_pair in catalog_ids), (
        f"probe catalog already lists {pin!r} verbatim; the cross-namespace "
        "journey cannot be staged on this host"
    )

    # Step 1-2: the user's session exists with the gateway-spelled pin
    # (spec pin / orchestrator pick / stale ``/model`` persist).
    session_id = _create_unbound_native_session(rig.base_url, harness)
    patch = _client.patch(
        f"{rig.base_url}/v1/sessions/{session_id}",
        json={"model_override": pin},
        timeout=10.0,
    )
    patch.raise_for_status()

    try:
        # Step 3: open the session in the web app, then let the runner bind
        # — the bind triggers the native terminal auto-create, i.e. the
        # launch whose gate is under test.
        page.goto(f"{rig.base_url}/c/{session_id}")
        bind = _client.patch(
            f"{rig.base_url}/v1/sessions/{session_id}",
            json={"runner_id": rig.runner_id},
            timeout=10.0,
        )
        bind.raise_for_status()

        # Outcome: the terminal registers (launch proceeded), or the launch
        # failure lands in the session's durable failure record.
        gate_error: str | None = None
        launched = False
        deadline = time.monotonic() + _OUTCOME_TIMEOUT_S
        while time.monotonic() < deadline:
            gate_error = _session_launch_error(rig.base_url, session_id)
            if gate_error is not None:
                break
            if _session_terminal_exists(rig.base_url, session_id):
                launched = True
                break
            time.sleep(2.0)

        if gate_error is not None:
            # Let the SPA render the failure the user sees (the error pill
            # driven by the ``session.status: failed`` edge) so a recorded
            # run ends on the observable outcome; never mask the primary
            # assertion if the pill lags.
            with contextlib.suppress(AssertionError):
                expect(page.locator(_ERROR_PILL).first).to_be_visible(timeout=30_000)
            pytest.fail(
                f"native {harness} terminal launch failed for a pin that denotes a "
                f"served model: pin={pin!r} (served by the host's launchable catalog "
                f"as {served!r}; rows={catalog_ids}). Launch error: {gate_error}"
            )

        assert launched, (
            f"native {harness} terminal neither launched nor hit the model gate "
            f"within {_OUTCOME_TIMEOUT_S:.0f}s (pin={pin!r}, catalog={catalog_ids}).\n"
            f"Runner log tail:\n{rig.runner_log.read_text()[-3000:]}"
        )

        # The fallback warning precedes the terminal launch, so by the time
        # the terminal has registered its absence proves the gate honored
        # the pin (folded onto the catalog's spelling) rather than silently
        # launching the provider default and resetting the pick.
        runner_log = rig.runner_log.read_text()
        assert _FALLBACK_MARKERS[harness] not in runner_log, (
            f"native {harness} terminal launched, but on the provider default: "
            f"the gate could not place the pin {pin!r} on the catalog (served as "
            f"{served!r}) and fell back instead of folding it.\n"
            f"Runner log tail:\n{runner_log[-3000:]}"
        )
    finally:
        with contextlib.suppress(httpx.HTTPError):
            _client.delete(f"{rig.base_url}/v1/sessions/{session_id}", timeout=10.0)
