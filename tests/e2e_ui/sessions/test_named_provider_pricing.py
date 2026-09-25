"""UI journey: a named-provider session must be priced at the named provider's rate.

Custom pricing resolves the DEFAULT provider for the session's harness family
(``default_provider_for_harness`` in ``omnigent/llms/context_window.py``) instead
of the provider the session was actually launched with
(``executor.auth: {type: provider}``). A session bound to a NAMED provider whose
custom rates differ from the family default is therefore priced at the DEFAULT
provider's rate.

Journey (real web SPA, live server + runner, openai-agents harness against the
mock ``/v1/responses``):

1. two openai-family providers are configured with different custom pricing --
   a cheap DEFAULT ($1/$1 per M tokens) and an expensive NAMED ($10/$10 per M)
2. an openai-agents agent is bound via ``executor.auth`` to the expensive NAMED
   provider, and a session is created for it
3. send a message; the turn completes through the named provider, reporting
   1,000,000 input and 1,000,000 output tokens
4. open the agent-info popover and read Session cost
5. observable failure: Session cost shows $2.00 (the cheap DEFAULT provider's
   rate) instead of $20.00 (the expensive NAMED provider's rate)

Regression guard: the final assertion (Session cost == $20.00, the named rate)
FAILS on the current build (it shows $2.00) and passes once pricing threads the
actual provider identity from session state. The turn-completes precondition and
the "a priced cost rendered at all" check pass both before and after a fix,
pinning the failure to mis-pricing rather than a broken turn or missing usage.
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.runner.identity import token_bound_runner_id
from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests.e2e_ui.conftest import (
    _BUILD_OUTPUT,
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _find_free_port,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

_FINAL_TEXT = "Hello from the named provider."

# The turn reports a flat 1,000,000 in / 1,000,000 out. At the cheap DEFAULT
# rate ($1/$1 per million) that is $2.00; at the expensive NAMED rate
# ($10/$10 per million) it is $20.00. The 10x gap makes the mis-pricing
# unambiguous and dominates the two-decimal display, so a stray tiny extra
# request cannot shift the rendered cost.
_USAGE = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
_DEFAULT_RATE_COST = "$2.00"
_NAMED_RATE_COST = "$20.00"


def _provider_config_yaml(mock_base_url: str, model: str) -> str:
    """Two openai-family providers at different custom rates.

    ``repro-default-cheap`` is the DEFAULT for the openai family (the one
    ``default_provider_for_harness`` resolves); ``repro-named-expensive`` is a
    non-default named provider the agent binds to via ``executor.auth``.
    """
    return f"""\
providers:
  repro-default-cheap:
    kind: key
    default: [openai]
    openai:
      base_url: "{mock_base_url}/v1"
      api_key: "mock-key"
      wire_api: responses
      models:
        default: {model}
      pricing:
        input_per_million: 1.0
        output_per_million: 1.0
  repro-named-expensive:
    kind: key
    openai:
      base_url: "{mock_base_url}/v1"
      api_key: "mock-key"
      wire_api: responses
      models:
        default: {model}
      pricing:
        input_per_million: 10.0
        output_per_million: 10.0
"""


def _agent_yaml(name: str, model: str) -> str:
    """A single-file openai-agents agent bound to the expensive NAMED provider."""
    return f"""\
name: {name}
prompt: You are a terse assistant. Say hello in one short sentence.

executor:
  harness: openai-agents
  model: {model}
  auth:
    type: provider
    name: repro-named-expensive
"""


def _agent_bundle(name: str, model: str) -> bytes:
    """Gzipped tarball of the agent spec.

    A non-``config.yaml`` archive name routes the bundle through the omnigent
    compat adapter, which accepts the ``executor.harness`` shorthand and
    preserves ``executor.auth`` (see ``_build_hello_world_bundle``).
    """
    yaml_bytes = _agent_yaml(name, model).encode()
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        return buf.getvalue()


@dataclass
class _NamedProviderServer:
    base_url: str
    runner_id: str
    model: str


@pytest.fixture
def named_provider_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[_NamedProviderServer]:
    """Spawn a server + runner whose config declares two custom-priced providers.

    A dedicated server (not the session-scoped ``live_server``) is required so
    ``OMNIGENT_CONFIG_HOME`` points at a config that carries the two providers'
    custom pricing before the server and runner start; that config is what both
    the launch-time provider resolution and the pricing-time default lookup read.
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("named-provider pricing e2e requires an isolated spawned server")

    server_tmp = tmp_path_factory.mktemp("e2e_ui_named_provider_pricing")
    config_home = server_tmp / "config-home"
    config_home.mkdir(parents=True, exist_ok=True)
    model = f"gpt-4o-mini-namedrate-{uuid.uuid4().hex[:8]}"
    (config_home / "config.yaml").write_text(
        _provider_config_yaml(mock_llm_server_url, model), encoding="utf-8"
    )

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = server_tmp / "server.log"
    runner_log_path = server_tmp / "runner.log"
    db_path = server_tmp / "test.db"
    artifact_dir = server_tmp / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    # The named provider (via ProviderAuth) governs the provider identity used
    # for launch routing and pricing; OPENAI_* only backstop any env fallback,
    # and both point at the same mock, so neither changes which rate applies.
    shared_env = {
        **os.environ,
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
        "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
    }
    apply_server_env(shared_env, _REPO_ROOT)

    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    server_command = [
        server_executable(),
        "-c",
        "import omnigent.server.presence as _p; _p._LEAVE_GRACE_S = 1.0; "
        + "from omnigent.cli import main; main()",
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--database-uri",
        f"sqlite:///{db_path}",
        "--artifact-location",
        str(artifact_dir),
    ]

    log_handle = open(log_path, "w")  # noqa: SIM115
    runner_log_handle = open(runner_log_path, "w")  # noqa: SIM115
    proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None

    def _wait_until_ready(
        server_process: subprocess.Popen[bytes],
        runner_process: subprocess.Popen[bytes],
    ) -> None:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if server_process.poll() is not None:
                last_error = f"server exited early with code {server_process.returncode}"
                break
            if runner_process.poll() is not None:
                last_error = f"runner exited early with code {runner_process.returncode}"
                break
            try:
                resp = httpx.get(f"{base_url}/health", timeout=2)
                if resp.status_code == 200:
                    status_resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status_resp.status_code == 200 and status_resp.json()["online"] is True:
                        return
                    last_error = (
                        f"runner status HTTP {status_resp.status_code}: {status_resp.text[:200]}"
                    )
                else:
                    last_error = f"health HTTP {resp.status_code}: {resp.text[:200]}"
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_HEALTH_POLL_INTERVAL_S)
        raise RuntimeError(
            f"named-provider pricing server did not become healthy within "
            f"{_HEALTH_TIMEOUT_S:.0f}s on {base_url} (last_error={last_error}).\n"
            f"Server log:\n{log_path.read_text()[-3000:] if log_path.exists() else ''}\n"
            "Runner log:\n"
            f"{runner_log_path.read_text()[-3000:] if runner_log_path.exists() else ''}"
        )

    try:
        proc = subprocess.Popen(
            server_command,
            env=server_env,
            cwd=compat_server_cwd(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_log_handle,
            stderr=subprocess.STDOUT,
        )
        _wait_until_ready(proc, runner_proc)

        # Guardrails classifier + any stray non-turn call (title inference,
        # model validation) get benign replies so they can't consume the
        # scripted turn; the turn itself is keyed to the agent's unique model.
        set_fallback_mock_llm(
            mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}'
        )
        set_fallback_mock_llm(mock_llm_server_url, "default", "OK")
        reset_mock_llm(mock_llm_server_url)
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": _FINAL_TEXT, "usage": _USAGE}],
            key=model,
        )
        yield _NamedProviderServer(base_url=base_url, runner_id=runner_id, model=model)
    finally:
        for p in (runner_proc, proc):
            if p is not None and p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=5)
        runner_log_handle.close()
        log_handle.close()


def _create_named_provider_session(base_url: str, runner_id: str, model: str) -> str:
    name = f"named-pricing-{uuid.uuid4().hex[:8]}"
    bundle = _agent_bundle(name, model)
    # A title in metadata suppresses background title inference, which would
    # otherwise consume the turn's single scripted response.
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"title": "Named provider pricing"})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = str(create_resp.json()["session_id"])
    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _send(page: Page, text: str) -> None:
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


@pytest.mark.timeout(600)
def test_named_provider_session_priced_at_named_rate(
    page: Page,
    named_provider_server: _NamedProviderServer,
    mock_llm_server_url: str,
) -> None:
    """A session bound to a NAMED provider must be priced at that provider's rate.

    The turn runs through ``repro-named-expensive`` ($10/$10 per M) and reports
    1,000,000 in / 1,000,000 out, so its true cost is $20.00. On the current
    build pricing resolves the openai-family DEFAULT (``repro-default-cheap``,
    $1/$1 per M) via ``default_provider_for_harness`` and shows $2.00 instead --
    the ``$20.00`` assertion fails. Once pricing threads the actual provider it
    passes.
    """
    base_url = named_provider_server.base_url
    model = named_provider_server.model

    session_id = _create_named_provider_session(base_url, named_provider_server.runner_id, model)
    try:
        page.goto(f"{base_url}/c/{session_id}")

        _send(page, "Say hello.")
        expect(page.locator(_ASSISTANT).filter(has_text=_FINAL_TEXT).first).to_be_visible(
            timeout=240_000
        )
        expect(page.locator(_WORKING)).to_have_count(0, timeout=240_000)

        # Precondition: the turn really ran through the named provider (one
        # model request carrying the scripted usage). Passes before and after a
        # fix, so a smaller cost below is mis-pricing, not a broken turn.
        reqs = httpx.get(
            f"{mock_llm_server_url}/mock/requests", params={"key": model}, timeout=10.0
        )
        reqs.raise_for_status()
        captured = reqs.json()["requests"]
        assert len(captured) >= 1, (
            f"expected at least one model request routed to {model!r}; the "
            f"openai-agents/named-provider wiring broke, not the pricing bug"
        )

        # Open the agent-info popover and read the Session cost row.
        trigger = page.get_by_test_id("agent-info-trigger")
        trigger.focus()
        trigger.press("Enter")
        panel = page.get_by_test_id("agent-info-panel")
        cost = panel.get_by_test_id("agent-info-session-cost")
        expect(cost).to_be_visible(timeout=30_000)
        # A priced cost rendered at all (not "<$0.01"): passes before and after
        # a fix, isolating the failure to the wrong rate rather than no pricing.
        expect(cost).to_have_text(re.compile(r"^\$\d"), timeout=30_000)
        assert cost.text_content() != _DEFAULT_RATE_COST, (
            "Session cost shows the cheap DEFAULT provider's rate "
            f"({_DEFAULT_RATE_COST}); it must reflect the NAMED provider "
            f"({_NAMED_RATE_COST}) the session actually ran through"
        )
        expect(cost).to_have_text(_NAMED_RATE_COST)
    finally:
        with httpx.Client() as client:
            client.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
