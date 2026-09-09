"""pi-native: a Claude-family model on an openai-only gateway silently 404s.

The journey
-----------
A user configures a ``kind: gateway`` provider (``default: [pi]``) that
exposes ONLY an ``openai`` family (``wire_api: chat``, the gateway's OpenAI
base URL) whose default model is a Claude-family id (``claude-fable-5-1`` -- a
model the gateway serves only on its *Anthropic* surface). They launch a
pi-native session (``omnigent pi``) and send a turn. Every turn fails with
``404 status code (no body)``, because pi-native renders a managed
``models.json`` with ``api: openai-completions`` and POSTs the Claude model id
to the gateway's ``/chat/completions`` endpoint.

Three defects, all in ``omnigent/harnesses/pi_native/credentials.py`` (the
report cites the pre-relocation path ``omnigent/pi_native_credentials.py``):

* **A -- silent cross-family fallthrough.** ``_inline_family_order`` tries the
  model's own family first (``claude`` -> ``anthropic``) but, when that family
  is not configured, deliberately falls through to ``openai``. That fallthrough
  is meant for protocol-translating proxies, but for a known-family id on a raw
  passthrough gateway it is wrong -- and ``unroutable_model_warning`` only fires
  for Databricks-primary configs, so a gateway provider gets *no warning*.
* **B -- unmanaged ``provider/`` prefix registered verbatim.**
  ``_split_pi_native_model_selection`` only splits ``provider/model`` when the
  prefix is a managed id (``omnigent*``). An override like
  ``rpw-fable/databricks-claude-fable-5-1`` is registered verbatim as a literal
  model id under provider ``omnigent`` -- same 404.
* **C -- latent prefix-stripping.** ``_inline_family_pi_provider`` hardcodes
  ``KEY_KIND`` in its ``normalize_model_for_provider`` call, so an override of
  ``databricks-claude-fable-5-1`` renders as ``claude-fable-5-1`` -- wrong for a
  gateway fronting Databricks AI Gateway, which expects the prefixed endpoint
  name. The family ``models.default`` path renders it correctly.

The fail -> pass contract
-------------------------
The runner surfaces ``credential_warning`` only as an *advisory* banner and
still launches pi with the ``openai-completions`` ``models.json``
(``omnigent/runner/native/orchestration.py``), so a warn-only fix still 404s
against the gateway. The assertions therefore key on the *silent* misroute --
``openai-completions`` **and** no routing warning -- exactly the report's
requested regression tests A/B/C. Each test fails on the current build and
passes after either a reroute fix or a warn/refuse fix.

The launch-render tests (B/C) render ``models.json`` through the exact runner
launch path (``resolve_pi_native_provider`` -> ``pi_native_provider_launch``);
the web-picker ``model_override`` delivery that carries those overrides is not
reachable through the plain ``omnigent pi`` CLI, so they are pinned at the
runner's launch-render boundary. The live journey test drives the real
``omnigent pi`` CLI end to end and observes the 404 against a fake gateway.

Usage::

    python -m pytest tests/e2e/test_pi_native_gateway_claude_404_e2e.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import pty
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.pi_native.credentials import (
    pi_native_provider_launch,
    resolve_pi_native_provider,
)
from omnigent.harnesses.pi_native.main import pi_bridge_dir_for_session
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.helpers import POLL_INTERVAL_S

# tests/e2e/<this file> -> parents[2] is the worktree root; threaded onto the
# CLI + runner subprocess PYTHONPATH so they import THIS worktree's code.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# A Claude-family model the gateway serves only on its Anthropic surface.
_CLAUDE_MODEL = "claude-fable-5-1"

# Wide PTY so the CLI's "Omnigent: <url>/c/<id>" line and pi's status bar (which
# echoes the resolved model) are not wrapped/truncated.
_PTY_ROWS = 50
_PTY_COLS = 220

# Env vars that, leaked from this (possibly omnigent-hosted) process into the pi
# subprocess, would misroute the runner or shadow the harness's own auth/route.
_STALE_ENV_VARS = (
    "DATABRICKS_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OMNIGENT_RUNNER_ID",
    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN",
    "RUNNER_SERVER_URL",
    "OMNIGENT_RUNNER_WORKSPACE",
    "TMUX",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)

# pi prints "Omnigent: <url>/c/<id>"; its id is a bare 32-hex. Keep the claude/
# codex "conv_<hex>" form too so the matcher is harness-agnostic.
_CONV_ID_RE = re.compile(r"/c/(conv_[0-9a-f]+|[0-9a-f]{32})")

# Budget for the full CLI journey: server auto-spawn + runner online + pi TUI
# boot + one turn against the loopback gateway. Generous for a loaded CI box.
_JOURNEY_TIMEOUT_S = 220


# --------------------------------------------------------------------------- #
# Config builders                                                             #
# --------------------------------------------------------------------------- #
def _gateway_config(*, base_url: str, with_anthropic: bool = False) -> dict:
    """Build a ``kind: gateway`` provider config as a loaded-config dict.

    :param base_url: The gateway root; the openai family points at
        ``{base_url}/openai`` and (optionally) the anthropic family at
        ``{base_url}/anthropic``.
    :param with_anthropic: Add an ``anthropic`` family block (the workaround
        config, used by facet C).
    :returns: A dict shaped like ``load_config()`` output.
    """
    provider: dict = {
        "kind": "gateway",
        "default": ["pi"],
        "openai": {
            "base_url": f"{base_url}/openai",
            "api_key": "test-gateway-key",
            "wire_api": "chat",
            "models": {"default": _CLAUDE_MODEL},
        },
    }
    if with_anthropic:
        provider["anthropic"] = {
            "base_url": f"{base_url}/anthropic",
            "api_key": "test-gateway-key",
            "models": {"default": "databricks-claude-fable-5-1"},
        }
    return {"providers": {"corp-gateway": provider}}


def _config_yaml(*, base_url: str) -> str:
    """The ``config.yaml`` for the live journey: openai-only gateway."""
    return (
        "providers:\n"
        "  corp-gateway:\n"
        "    kind: gateway\n"
        "    default: [pi]\n"
        "    openai:\n"
        f"      base_url: {base_url}/openai\n"
        "      api_key: test-gateway-key\n"
        "      wire_api: chat\n"
        "      models:\n"
        f"        default: {_CLAUDE_MODEL}\n"
    )


def _render_runner_models_json(*, model: str, with_anthropic: bool) -> tuple[dict, object]:
    """Render the per-session ``models.json`` exactly as the runner does.

    Mirrors ``omnigent/runner/native/orchestration.py``:
    ``resolve_pi_native_provider(model=...)`` then
    ``pi_native_provider_launch(<agent_dir>, provider, selection=model)``.

    :returns: ``(models_json_dict, provider)``.
    """
    cfg = _gateway_config(base_url="https://gw.invalid", with_anthropic=with_anthropic)
    provider = resolve_pi_native_provider(model=model, config_loader=lambda: cfg)
    assert provider is not None, f"provider unexpectedly unresolved for model {model!r}"
    agent_dir = Path(tempfile.mkdtemp(prefix="pi-agent-"))
    pi_native_provider_launch(agent_dir, provider, selection=model)
    return json.loads((agent_dir / "models.json").read_text()), provider


def _model_ids(models_json: dict) -> list[str]:
    """Every model id declared across all providers in a ``models.json``."""
    ids: list[str] = []
    for prov in (models_json.get("providers") or {}).values():
        for entry in prov.get("models") or []:
            mid = entry.get("id")
            if isinstance(mid, str):
                ids.append(mid)
    return ids


def _provider_apis(models_json: dict) -> list[str]:
    """Every provider ``api`` (wire protocol) in a ``models.json``."""
    return [prov.get("api") for prov in (models_json.get("providers") or {}).values()]


# --------------------------------------------------------------------------- #
# Fake gateway (a raw passthrough gateway)                                    #
# --------------------------------------------------------------------------- #
class _FakeGatewayHandler(BaseHTTPRequestHandler):
    """Play a raw passthrough gateway fronting a model inventory.

    - ``GET`` (model-inventory probes) -> ``200`` with an empty inventory.
    - ``POST`` (a turn) -> ``404`` with NO body, exactly as the reported
      gateway answers a Claude model id posted to its OpenAI
      ``/chat/completions`` surface.
    """

    protocol_version = "HTTP/1.1"
    # Populated by the fixture: (method, path) of every request the CLI sent.
    requests_seen: list[tuple[str, str]] = []

    def _drain(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        while length > 0:
            chunk = self.rfile.read(min(length, 65536))
            if not chunk:
                break
            length -= len(chunk)

    def do_GET(self) -> None:
        self._drain()
        type(self).requests_seen.append(("GET", self.path))
        body = json.dumps({"data": [], "models": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self._drain()
        type(self).requests_seen.append(("POST", self.path))
        # 404 with no body -- the reported symptom.
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        pass


@pytest.fixture
def fake_gateway() -> Iterator[str]:
    """Run the fake gateway on a free loopback port.

    :yields: The gateway root URL, e.g. ``"http://127.0.0.1:44587"``.
    """
    _FakeGatewayHandler.requests_seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGatewayHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# Facet A -- resolution contract (deterministic)                              #
# --------------------------------------------------------------------------- #
def test_facet_a_openai_only_gateway_claude_model_not_silently_openai() -> None:
    """A: a Claude model on an openai-only gateway must not SILENTLY route to
    ``openai-completions``.

    Report's regression test A: assert resolution does not silently yield
    ``api == "openai-completions"`` without the new routing warning (or returns
    ``None`` / reroutes). Fails now (api is ``openai-completions`` with no
    warning); passes after a reroute or a warn/refuse fix.
    """
    cfg = _gateway_config(base_url="https://gw.invalid")
    provider = resolve_pi_native_provider(model=_CLAUDE_MODEL, config_loader=lambda: cfg)

    silent_misroute = (
        provider is not None
        and provider.api == "openai-completions"
        and not (provider.credential_warning or provider.unroutable_model_warning())
    )
    assert not silent_misroute, (
        f"Claude-family model {_CLAUDE_MODEL!r} on a kind:gateway provider "
        "configured with only an openai family silently resolved to "
        f"api='openai-completions' (base_url="
        f"{getattr(provider, 'base_url', None)!r}) with NO routing warning "
        f"(credential_warning={getattr(provider, 'credential_warning', None)!r}, "
        f"unroutable_model_warning="
        f"{provider.unroutable_model_warning() if provider else None!r}). pi then "
        "POSTs a Claude model id to /chat/completions and every turn 404s. The "
        "fix must reroute to a Claude-capable family or surface a routing "
        "warning, never fall through to the other family silently."
    )


# --------------------------------------------------------------------------- #
# Facet B -- unmanaged provider/ prefix registered verbatim                   #
# --------------------------------------------------------------------------- #
def test_facet_b_unmanaged_provider_prefix_not_registered_verbatim() -> None:
    """B: a ``provider/`` override with an unmanaged prefix must not be
    registered verbatim as a literal model id.

    Report's regression test B: assert the rendered ``models.json`` never
    contains a model id carrying an unmanaged ``provider/`` prefix. Fails now
    (id is ``rpw-fable/databricks-claude-fable-5-1``); passes once the resolver
    strips-and-warns or rejects the unmanaged prefix.
    """
    models_json, _ = _render_runner_models_json(
        model="rpw-fable/databricks-claude-fable-5-1", with_anthropic=False
    )
    ids = _model_ids(models_json)
    slashed = [mid for mid in ids if "/" in mid]
    assert not slashed, (
        "An override carrying an unmanaged 'provider/' prefix "
        "('rpw-fable/databricks-claude-fable-5-1') was registered verbatim as a "
        f"literal model id in the rendered models.json: {slashed!r}. Managed pi "
        "sessions relocate PI_CODING_AGENT_DIR and never read ~/.pi/agent, so "
        "this slash id has no provider and every turn 404s. The fix must "
        "strip-and-warn or reject unmanaged provider/ prefixes in "
        "resolve_pi_native_provider."
    )


# --------------------------------------------------------------------------- #
# Facet C -- databricks- prefix stripped on an anthropic-family gateway       #
# --------------------------------------------------------------------------- #
def test_facet_c_gateway_anthropic_family_keeps_databricks_prefix() -> None:
    """C: an override of ``databricks-claude-fable-5-1`` against a gateway with
    an anthropic family must be sent verbatim.

    Report's regression test C: assert the id is not prefix-stripped. Fails now
    (renders ``claude-fable-5-1`` because ``KEY_KIND`` is hardcoded in
    ``normalize_model_for_provider``); passes once the entry's real kind
    (``GATEWAY_KIND`` pass-through) is used. The family ``models.default`` path
    already renders the prefixed id correctly -- only the override path strips.
    """
    models_json, _ = _render_runner_models_json(
        model="databricks-claude-fable-5-1", with_anthropic=True
    )
    ids = _model_ids(models_json)
    assert "databricks-claude-fable-5-1" in ids, (
        "Override 'databricks-claude-fable-5-1' against a gateway with an "
        f"anthropic family rendered model ids {ids!r} -- the 'databricks-' "
        "prefix was stripped (KEY_KIND normalization hardcoded in "
        "_inline_family_pi_provider), but a gateway fronting Databricks AI "
        "Gateway expects the prefixed endpoint name. The fix must pass the "
        "entry's real kind (treat GATEWAY_KIND as pass-through) to "
        "normalize_model_for_provider."
    )


# --------------------------------------------------------------------------- #
# Facet A -- full live CLI journey (omnigent pi -> 404)                        #
# --------------------------------------------------------------------------- #
def _match_conv(output: str) -> str | None:
    """Return the conversation id from CLI output, or ``None``."""
    match = _CONV_ID_RE.search(output)
    return match.group(1) if match else None


def _wait_for(
    predicate: Callable[[], object],
    *,
    timeout: float,
    what: str,
    tail: Callable[[], str],
) -> object:
    """Poll *predicate* until it returns a truthy value, else fail with output.

    :returns: The first truthy value *predicate* produced.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"{what} did not appear within {timeout}s; CLI output tail:\n{tail()[-2500:]}"
    )


def _is_credential_warning(item: dict) -> bool:
    """True when a session item is the pi-credentials routing warning banner."""
    return "pi_credentials_unresolved" in json.dumps(item)


@pytest.mark.skipif(
    (_PI_REASON := cli_unavailable_reason("pi")) is not None,
    reason=(
        f"pi-native gateway 404 journey requires a runnable 'pi' CLI; {_PI_REASON}. "
        "Install/fix Pi to run this test."
    ),
)
@pytest.mark.timeout(_JOURNEY_TIMEOUT_S + 120)
def test_facet_a_live_pi_cli_gateway_claude_404_journey(fake_gateway: str) -> None:
    """Drive the real ``omnigent pi`` CLI journey and observe the silent 404.

    Journey: configure an openai-only ``kind: gateway`` provider whose default
    model is ``claude-fable-5-1`` -> ``omnigent pi`` (auto-spawns a local server
    + runner + the real Pi TUI) -> send a turn -> the runner-rendered
    ``models.json`` wires ``api: openai-completions`` and the turn is POSTed to
    the gateway's ``/chat/completions`` surface, which 404s.

    Durable contract: the runner must not SILENTLY wire a Claude model to
    ``openai-completions``. Fails now (openai-completions + no warning + gateway
    404); passes after a reroute fix (api changes) or a warn fix (a
    ``pi_credentials_unresolved`` banner is surfaced to the session).
    """
    config_home = Path(tempfile.mkdtemp(prefix="pi-gw-config-"))
    (config_home / "config.yaml").write_text(_config_yaml(base_url=fake_gateway), encoding="utf-8")

    env = dict(os.environ)
    for stale in _STALE_ENV_VARS:
        env.pop(stale, None)
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["TERM"] = "xterm-256color"
    env["LINES"] = str(_PTY_ROWS)
    env["COLUMNS"] = str(_PTY_COLS)
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"

    omnigent = Path(sys.executable).parent / "omnigent"
    assert omnigent.is_file(), f"omnigent console script not found at {omnigent}"

    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.execve(str(omnigent), [str(omnigent), "pi", "--server", ""], env)
        except OSError:
            os._exit(127)

    buf: list[bytes] = []
    lock = threading.Lock()
    stop = threading.Event()

    def _drain() -> None:
        while not stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
            except OSError:
                break
            if not ready:
                continue
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            with lock:
                buf.append(data)

    def _output() -> str:
        with lock:
            return b"".join(buf).decode("utf-8", "replace")

    threading.Thread(target=_drain, name=f"pi-pty-drain-{pid}", daemon=True).start()

    try:
        # 1. The CLI prints "Omnigent: <url>/c/<conv>" once the session exists.
        conv = str(
            _wait_for(
                lambda: _match_conv(_output()), timeout=160, what="conversation id", tail=_output
            )
        )
        bridge = pi_bridge_dir_for_session(conv)
        models_path = bridge / "pi-agent" / "models.json"

        # 2. Wait for the runner to render the managed per-session models.json.
        _wait_for(
            lambda: models_path.exists() or None,
            timeout=90,
            what="rendered models.json",
            tail=_output,
        )
        rendered = json.loads(models_path.read_text())
        rendered_apis = _provider_apis(rendered)
        rendered_ids = _model_ids(rendered)

        # 3. Drive a real turn via the web-UI path (POST /events), using the
        #    bridge's own serverUrl + auth headers (as the web UI does).
        bridge_cfg = json.loads((bridge / "config.json").read_text())
        server_url = bridge_cfg.get("serverUrl")
        auth_headers = bridge_cfg.get("authHeaders") or {}
        assert server_url, f"bridge config.json missing serverUrl: {bridge_cfg!r}"

        with httpx.Client(timeout=30.0, trust_env=False) as client:
            resp = client.post(
                f"{server_url}/v1/sessions/{conv}/events",
                headers=auth_headers,
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Reply with exactly: PONG"}],
                    },
                },
            )
            resp.raise_for_status()

            def _gateway_chat_hit() -> bool:
                return any(
                    method == "POST" and "chat/completions" in path
                    for method, path in _FakeGatewayHandler.requests_seen
                )

            def _warning_surfaced() -> bool:
                items_resp = client.get(
                    f"{server_url}/v1/sessions/{conv}/items", headers=auth_headers
                )
                if items_resp.status_code != 200:
                    return False
                items = items_resp.json().get("data", [])
                return any(_is_credential_warning(it) for it in items)

            # Wait until the turn is processed: either it reached the gateway's
            # openai-completions surface, or a routing warning was surfaced.
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                if _gateway_chat_hit() or _warning_surfaced():
                    break
                time.sleep(POLL_INTERVAL_S)

            gateway_chat_hit = _gateway_chat_hit()
            warning_surfaced = _warning_surfaced()
    finally:
        stop.set()
        # Tear down the whole tree: the CLI's process group (CLI + tmux attach),
        # then the auto-spawned managed server + local daemon + runner.
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            os.waitpid(pid, 0)
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(Exception):
            subprocess.run(
                [str(omnigent), "server", "stop"],
                env=env,
                capture_output=True,
                timeout=60,
            )

    silently_openai = "openai-completions" in rendered_apis
    assert (not silently_openai) or warning_surfaced, (
        f"pi-native launched the Claude model {_CLAUDE_MODEL!r} on an openai-only "
        "gateway by SILENTLY wiring api='openai-completions' and every turn 404s.\n"
        f"  runner-rendered models.json apis = {rendered_apis!r}\n"
        f"  runner-rendered models.json ids  = {rendered_ids!r}\n"
        f"  gateway received POST .../chat/completions = {gateway_chat_hit}\n"
        f"  routing warning surfaced to session        = {warning_surfaced}\n"
        "The fix must reroute the Claude model to a Claude-capable family or "
        "surface a routing warning (pi_credentials_unresolved) -- never render "
        "openai-completions for a Claude model with no warning."
    )
