"""E2E: a ``kind: databricks`` provider's ``models:`` map pins the served model.

Proves the end of the per-tier-model-override feature (see
``feat(providers): per-tier model overrides on the databricks provider kind``)
against a *live* Databricks workspace: a ``models: {default: <endpoint>}`` entry
in ``~/.omnigent/config.yaml`` reaches
:func:`omnigent.runtime.workflow.configure_agent_harness_with_ucode` at spawn
and lands in ``HARNESS_CLAUDE_SDK_MODEL``, so the claude-sdk subprocess runs its
turn on the overridden endpoint rather than the hardcoded Databricks default.

The override target is pinned to ``databricks-claude-sonnet-4-6`` *specifically*
because it differs from the harness's hardcoded Databricks fallback
(:data:`omnigent.onboarding.databricks_config.DATABRICKS_CLAUDE_DEFAULT_MODEL` =
``databricks-claude-opus-4-8``). A test that pinned the fallback value could
pass without the override doing anything; pinning a *different* Claude tier
(sonnet, not opus) makes the assertion fail loud if the map is ignored and the
spawn falls back to opus.

Isolation contract (why this file spawns its own server + runner):
    The shared ``live_server`` / ``http_client`` fixtures inherit ``os.environ``
    and do NOT redirect ``OMNIGENT_CONFIG_HOME``, so they read the developer's
    real ``~/.omnigent/config.yaml``. This test seeds a throwaway config home
    with the provider entry under test and spawns its OWN server and runner
    subprocesses pointed at it. The runner is the process that builds the
    claude-sdk spawn env (``_build_claude_sdk_spawn_env`` →
    ``configure_agent_harness_with_provider`` → ``configure_agent_harness_with_ucode``),
    so the runner is the one that must see the seeded ``OMNIGENT_CONFIG_HOME``.
    The developer's real ``~/.omnigent``, ``~/.ucode/state.json``, and
    Databricks CLI config are never written.

How the served-model assertion works (which response field):
    The claude-sdk executor reports the model the SDK actually used as
    ``usage["model"]`` (``observed_model or model``) on every turn
    (``omnigent/inner/claude_sdk_executor.py``). The runner accumulates that
    into the session's per-model usage map keyed by the *raw harness-reported
    model id* (``_model_usage_bucket`` in ``omnigent/server/routes/sessions.py``
    — "alias normalization is intentionally deferred"), which surfaces as
    ``usage_by_model`` on ``GET /v1/sessions/{id}`` (:class:`SessionResponse`).
    So the served model is observable end-to-end: the ``usage_by_model`` keys
    are the model(s) that actually ran the turn. The assertion is a
    *discriminator*, not a brittle equality: the served model must identify the
    Sonnet override and must NOT identify the Opus fallback. The Databricks
    gateway may echo the requested id (``databricks-claude-sonnet-4-6``) or a
    canonical Anthropic name (``claude-sonnet-4-…``); both satisfy
    "contains sonnet, not opus", which is exactly the feature's contract
    (override beat the hardcoded default).

What breaks if this is wrong:
    - The override never reaches the spawn env → the turn runs on the opus
      fallback → ``usage_by_model`` carries an opus key → this test fails.
    - A spec-pinned model would outrank the map (precedence is spec > provider
      ``models:`` > ucode > default), so the agent registered here declares NO
      model; a regression that starts stamping a default model onto no-model
      specs would surface as a non-sonnet served model.

Usage::

    PROFILE=<your-databricks-profile>
    TOKEN=$(databricks auth token --profile "$PROFILE" | jq -r .access_token)
    env -u OPENAI_API_KEY OMNIGENT_SKIP_WEB_UI=true \\
      uv run --no-sync pytest \\
        tests/e2e/test_databricks_models_override_e2e.py \\
        --llm-api-key="$TOKEN" --profile="$PROFILE" -x -q

If your shell exports a workspace PAT env var, unset it first so the seeded
profile's auth wins over ambient credentials.

Skips cleanly (no-op) without ``--profile`` (mock mode has no Databricks
gateway to route to) or when the ``claude`` CLI is not installed.
"""

from __future__ import annotations

import io
import os
import secrets
import signal
import subprocess
import tarfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.onboarding.databricks_config import DATABRICKS_CLAUDE_DEFAULT_MODEL
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from tests._helpers.compat import (
    apply_runner_env,
    apply_server_env,
    compat_runner_cwd,
    compat_server_cwd,
    runner_executable,
    server_executable,
)
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.conftest import (
    create_runner_bound_session,
    find_free_port,
    poll_session_until_terminal,
    send_user_message_to_session,
)
from tests.e2e.helpers import HEALTH_TIMEOUT_S, POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The tier→model override under test. Pinned Sonnet on purpose: it differs from
# the hardcoded Databricks fallback below, so the assertion can't pass by
# accident if the override is dropped.
_OVERRIDE_MODEL = "databricks-claude-sonnet-4-6"
# The value the spawn would fall back to WITHOUT the override (the harness's
# hardcoded Databricks Claude default). Asserting the served model is NOT this
# is what proves the override actually took effect.
_FALLBACK_MODEL = DATABRICKS_CLAUDE_DEFAULT_MODEL  # "databricks-claude-opus-4-8"


def _register_no_model_claude_sdk_agent(
    client: httpx.Client,
    *,
    name: str,
    profile: str,
    prompt: str,
) -> str:
    """Register a claude-sdk agent that declares NO model, only a profile.

    Mirrors :func:`tests.e2e.conftest.register_inline_agent`'s multipart upload,
    but deliberately omits ``executor.model`` — that helper always stamps a
    resolved model, which (precedence spec > provider ``models:`` > ucode >
    default) would outrank the override this test exercises. With no spec model,
    ``_resolve_spec_model`` returns ``None``, ``HARNESS_CLAUDE_SDK_MODEL`` stays
    unset entering ``configure_agent_harness_with_ucode``, and the provider
    ``models: {default: …}`` map is what fills it.

    :param client: HTTP client pointed at the seeded server.
    :param name: Agent name.
    :param profile: Databricks CLI config profile the legacy
        ``executor.profile`` field carries; routes the spawn through the
        synthesized databricks provider → the ucode/gateway path.
    :param prompt: System prompt for the agent.
    :returns: The registered agent name.
    """
    import json as _json

    config: dict[str, Any] = {
        "name": name,
        "prompt": prompt,
        # No ``model`` key: the provider ``models:`` map is the only model
        # source, which is the code path under test.
        "executor": {"harness": "claude-sdk", "profile": profile},
    }
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.dump(config).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        bundle = buf.getvalue()

    resp = client.post(
        "/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"agent register failed: {resp.status_code} {resp.text[:500]}")
    return name


def _wait_for_usage_by_model(
    client: httpx.Client,
    session_id: str,
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Poll ``GET /v1/sessions/{id}`` until ``usage_by_model`` is populated.

    Per-model usage accrues from the turn's ``response.completed`` usage, which
    the runner may persist a beat after the turn is otherwise terminal. Poll so
    the assertion doesn't race the write.

    :param client: HTTP client pointed at the seeded server.
    :param session_id: The runner-bound session id.
    :param timeout: Max seconds to wait for a non-empty ``usage_by_model``.
    :returns: The ``usage_by_model`` map, or ``{}`` if none appeared in time.
    """
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        last = resp.json().get("usage_by_model") or {}
        if last:
            return last
        time.sleep(POLL_INTERVAL_S)
    return last


@pytest.fixture
def override_config_stack(
    request: pytest.FixtureRequest,
    llm_api_key: str,
    databricks_workspace_host: str | None,
    tmp_path: Path,
) -> Iterator[tuple[str, str, str]]:
    """Spawn an isolated server + runner over a seeded ``OMNIGENT_CONFIG_HOME``.

    Seeds a throwaway config home whose sole ``providers:`` entry is a
    ``kind: databricks`` provider carrying the ``models: {default: …}`` override,
    then spawns a server and a sibling runner (bound by a shared tunnel token,
    like ``live_server``) both pointed at that config home. The runner is where
    the claude-sdk spawn env is built, so it must see the seeded config.

    Skips (no-op) when ``--profile`` is absent — mock mode has no Databricks
    gateway to route to — or when the ``claude`` CLI is missing/unrunnable.

    :param request: pytest request — reads ``--profile``.
    :param llm_api_key: Databricks bearer from ``--llm-api-key`` (satisfies the
        server's startup env; the claude-sdk turn authenticates via the profile).
    :param databricks_workspace_host: Workspace host, or ``None`` (mock mode).
    :param tmp_path: Per-test temp dir for the config home, DB, artifacts, logs.
    :yields: ``(base_url, runner_id, profile)``.
    """
    profile: str = request.config.getoption("--profile")
    if not profile or databricks_workspace_host is None:
        pytest.skip(
            "databricks models-override e2e requires --profile <name> and a real "
            "--llm-api-key (a live Databricks gateway to route the turn through); "
            "it is a no-op in mock mode."
        )
    reason = cli_unavailable_reason("claude")
    if reason is not None:
        pytest.skip(f"claude-sdk harness requires a runnable 'claude' CLI; {reason}.")

    # ── Seed the throwaway config home with the provider under test ──────
    config_home = tmp_path / "omnigent_config_home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "databricks": {
                        "kind": "databricks",
                        "profile": profile,
                        "models": {"default": _OVERRIDE_MODEL},
                    }
                }
            }
        )
    )

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "override_e2e.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    server_log = tmp_path / "server.log"
    runner_log = tmp_path / "runner.log"

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    # Shared base env, mirroring live_server's Databricks-profile wiring.
    base_env = {
        **os.environ,
        "OPENAI_API_KEY": llm_api_key,
        "OPENAI_BASE_URL": f"{databricks_workspace_host}/serving-endpoints",
        "DATABRICKS_CONFIG_PROFILE": profile,
        # The isolation contract: config reads resolve here, not ~/.omnigent.
        "OMNIGENT_CONFIG_HOME": str(config_home),
    }

    # Server-side policy classifier needs a gateway-routed llm: block (else it
    # defaults to api.openai.com and 401s under a Databricks bearer), same as
    # live_server does under --profile.
    server_cfg = tmp_path / "server.yaml"
    server_cfg.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "model": "databricks-gpt-5-4-mini",
                    "connection": {
                        "base_url": f"{databricks_workspace_host}/serving-endpoints",
                        "api_key": llm_api_key,
                    },
                }
            }
        )
    )

    server_env = apply_server_env({**base_env}, _REPO_ROOT)
    server_log_handle = open(server_log, "w")  # noqa: SIM115 — lives for the Popen lifetime
    server_proc = subprocess.Popen(
        [
            server_executable(),
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
            "--config",
            str(server_cfg),
        ],
        env={**server_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token},
        cwd=compat_server_cwd(),
        stdout=server_log_handle,
        stderr=subprocess.STDOUT,
    )

    runner_env = apply_runner_env(
        {
            **base_env,
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base_url,
        }
    )
    runner_log_handle = open(runner_log, "w")  # noqa: SIM115
    runner_proc = subprocess.Popen(
        [runner_executable(), "-m", "omnigent.runner._entry"],
        env=runner_env,
        cwd=compat_runner_cwd(),
        stdout=runner_log_handle,
        stderr=subprocess.STDOUT,
    )

    try:
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                health = httpx.get(f"{base_url}/health", timeout=2)
                status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                if (
                    health.status_code == 200
                    and status.status_code == 200
                    and status.json().get("online") is True
                ):
                    break
            except httpx.HTTPError:
                pass
            if server_proc.poll() is not None:
                break
            time.sleep(POLL_INTERVAL_S)
        else:
            raise RuntimeError(
                f"server+runner didn't come online within {HEALTH_TIMEOUT_S}s.\n"
                f"server log:\n{server_log.read_text()[-3000:]}\n"
                f"runner log:\n{runner_log.read_text()[-3000:]}"
            )
        if server_proc.poll() is not None:
            raise RuntimeError(
                f"server exited early (code {server_proc.returncode}); "
                f"log tail:\n{server_log.read_text()[-3000:]}"
            )
        yield base_url, runner_id, profile
    finally:
        for proc in (runner_proc, server_proc):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        runner_log_handle.close()
        server_log_handle.close()


def test_databricks_models_override_serves_pinned_endpoint(
    override_config_stack: tuple[str, str, str],
) -> None:
    """A ``models: {default: …}`` map pins the live claude-sdk served model.

    Registers a claude-sdk agent with NO model (only the profile), runs one
    turn, and proves the turn ran on the Sonnet override — not the hardcoded
    Opus fallback — via the session's ``usage_by_model`` (keyed by the raw
    harness-reported served model). See the module docstring for the full
    signal path and why the assertion is a sonnet/not-opus discriminator.

    :param override_config_stack: ``(base_url, runner_id, profile)`` for the
        server+runner spawned over the seeded config home.
    """
    base_url, runner_id, profile = override_config_stack

    with httpx.Client(
        base_url=base_url,
        timeout=300,
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    ) as client:
        agent_name = _register_no_model_claude_sdk_agent(
            client,
            name=f"dbx-models-override-{uuid.uuid4().hex[:6]}",
            profile=profile,
            prompt="You are a terse assistant. Follow instructions exactly.",
        )
        session_id = create_runner_bound_session(
            client, agent_name=agent_name, runner_id=runner_id
        )
        response_id = send_user_message_to_session(
            client,
            session_id=session_id,
            content="Reply with exactly: OVERRIDE-OK",
        )

        body = poll_session_until_terminal(
            client,
            session_id=session_id,
            response_id=response_id,
            timeout=300,
        )
        assert body["status"] == "completed", (
            f"turn did not complete: {body.get('error', 'unknown error')}"
        )

        # A real turn ran (the reply proves the round-trip, not just a spawn).
        text = "\n".join(
            block.get("text", "")
            for item in body.get("output", [])
            if item.get("type") == "message"
            for block in item.get("content", [])
        )
        assert "OVERRIDE-OK" in text, f"model did not follow the prompt; got: {text[:500]!r}"

        # The served-model signal: usage_by_model is keyed by the model that
        # actually ran the turn.
        usage_by_model = _wait_for_usage_by_model(client, session_id)
        assert usage_by_model, (
            "session reported no usage_by_model; cannot confirm the served model. "
            "The turn completed but per-model usage never surfaced — investigate "
            "the runner usage-accumulation path before trusting this result."
        )
        served_models = list(usage_by_model.keys())

        # The override took effect iff a Sonnet endpoint served the turn and the
        # Opus fallback did not. Both the requested id and a canonical Anthropic
        # name contain "sonnet"; the fallback (databricks-claude-opus-4-8)
        # contains "opus".
        assert any("sonnet" in m.lower() for m in served_models), (
            f"expected the turn to run on the Sonnet override ({_OVERRIDE_MODEL!r}); "
            f"served model(s): {served_models}. If a non-sonnet model served the "
            f"turn, the provider models: override did not reach the spawn env."
        )
        assert not any("opus" in m.lower() for m in served_models), (
            f"the turn ran on an Opus model {served_models}, i.e. the hardcoded "
            f"fallback {_FALLBACK_MODEL!r} — the provider models: override was "
            f"ignored (the exact regression this test guards)."
        )
