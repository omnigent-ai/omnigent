"""E2E: pi-native model pickers must honor pi's own ``enabledModels`` curation.

Both facets share one precondition: a host whose Pi is logged in from its own
``~/.pi/agent`` into SEVERAL providers at once (``auth.json``: anthropic,
openai, google, openrouter — with openrouter's multi-vendor catalog seeded in
``models-store.json``), whose ``settings.json`` curates the usable set with
``enabledModels``, and which has NO omnigent-managed provider
(``resolve_pi_native_provider()`` returns ``None``). Pi's own Ctrl+P model
cycling honors ``enabledModels``; the catalogs Omnigent surfaces do not.

Facet A (in-session picker, surface ``web``):
    the running Pi's extension (``omnigent_pi_native_extension.js``
    ``postModelOptions``) pushes ``registry.getAvailable()`` — ALL models of
    EVERY authed provider — as ``external_model_options``, so the session's
    composer Model picker lists the union of every logged-in provider's full
    catalog (with openrouter authed: hundreds of rows from every vendor),
    ignoring pi's ``enabledModels``. Asserted through the same session
    snapshot field the SPA renders from (``GET /v1/sessions/{id}`` →
    ``model_options``).

Facet B (pre-launch picker, surface ``web``):
    ``GET /v1/hosts/{id}/harnesses/pi-native/model-options`` (the Configure-Pi
    dialog's catalog, ``pi_own_login_model_options()``) returns the same
    union — every model of every provider present in ``auth.json`` — again
    ignoring ``enabledModels``.

Both assertions are written against the FIXED behavior (an ``enabledModels``
curation, when set, scopes what the pickers offer; the multi-vendor
openrouter catalog must not clobber the list), so this module is RED on the
buggy build and turns GREEN once the pickers honor pi's own curation. It
runs against the mock LLM (no real credentials — auth entries are fakes and
no turn is driven), but launching the real Pi terminal needs ``pi`` /
``tmux`` / ``node`` on PATH; the module skips cleanly when any is absent.

    .venv/bin/python -m pytest tests/e2e/test_pi_native_picker_enabled_models.py -v
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import tarfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import POLL_INTERVAL_S
from tests.e2e.test_pi_native_unmanaged_model import (
    _bridge_marker,
    _kill_pi_processes,
    _UnmanagedPiHost,
    _wait_for_host_online,
)
from tests.e2e.test_pi_native_unmanaged_model import pytestmark as _pi_toolchain_marks

# Same toolchain gates as the sibling unmanaged-Pi module: pi + tmux + node.
pytestmark = _pi_toolchain_marks

# Worktree root (this file lives at <worktree>/tests/e2e/): absolute
# PYTHONPATH entries for the daemon-spawned runner (cwd = the workspace).
_WORKTREE = Path(__file__).resolve().parents[2]

# Pi's own curation: the ONE model the user enabled in pi's settings.json.
# Pi's Ctrl+P picker cycles exactly this; the Omnigent pickers must not offer
# a thousand-row union around it.
_ENABLED_MODEL = "anthropic/claude-sonnet-4-5"

# The multi-vendor catalog a real openrouter login maintains in
# models-store.json — the report names "Mercury, Ling, Nex AGI, Mistral
# `:batch` variants, 'Z.ai: GLM 5.3 (batch)'-style names" among hundreds.
_OPENROUTER_VENDORS: list[tuple[str, str]] = [
    ("mercury", "Mercury"),
    ("ling", "Ling"),
    ("nex-agi", "Nex AGI"),
    ("mistralai", "Mistral"),
    ("z-ai", "Z.ai"),
    ("qwen", "Qwen"),
    ("deepseek", "DeepSeek"),
    ("moonshotai", "MoonshotAI"),
    ("minimax", "MiniMax"),
    ("cohere", "Cohere"),
    ("ai21", "AI21"),
    ("meta-llama", "Meta"),
]
_OPENROUTER_SERIES = ["coder-large", "chat-v2", "instruct-3", "reasoning-pro"]
_OPENROUTER_SUFFIXES = [("", ""), (":batch", " (batch)"), (":free", " (free)")]


def _openrouter_models() -> list[dict[str, object]]:
    """Build the openrouter provider's multi-vendor model catalog.

    12 vendors x 4 series x 3 billing variants = 144 models — the "hundreds
    of models from every vendor" shape a real openrouter login yields.

    :returns: models-store.json ``models`` entries for provider
        ``openrouter``.
    """
    models: list[dict[str, object]] = []
    for slug, label in _OPENROUTER_VENDORS:
        for series in _OPENROUTER_SERIES:
            for id_suffix, name_suffix in _OPENROUTER_SUFFIXES:
                models.append(
                    {
                        "id": f"{slug}/{slug}-{series}{id_suffix}",
                        "name": f"{label}: {series.replace('-', ' ').title()}{name_suffix}",
                        "api": "openai-completions",
                        "provider": "openrouter",
                        "baseUrl": "https://openrouter.ai/api/v1",
                        "input": ["text"],
                    }
                )
    return models


def _first_party_models(
    provider: str, base_url: str, ids: list[tuple[str, str]]
) -> list[dict[str, object]]:
    """Build a first-party provider's models-store entries.

    :param provider: Pi provider id, e.g. ``"anthropic"``.
    :param base_url: Provider API base URL.
    :param ids: ``(model_id, display_name)`` pairs.
    :returns: models-store.json ``models`` entries.
    """
    return [
        {
            "id": model_id,
            "name": name,
            "api": "anthropic-messages" if provider == "anthropic" else "openai-completions",
            "provider": provider,
            "baseUrl": base_url,
            "input": ["text", "image"],
        }
        for model_id, name in ids
    ]


def _seed_multi_login_pi_home(home: Path) -> str:
    """Seed *home* with a multi-provider Pi login, curated by enabledModels.

    Writes ``.pi/agent/auth.json`` (four providers logged in, openrouter
    among them), ``.pi/agent/models-store.json`` (each login's catalog —
    openrouter's spanning every vendor), and ``.pi/agent/settings.json``
    with ``enabledModels`` scoping pi to exactly ``_ENABLED_MODEL``.
    ``.omnigent/config.yaml`` carries only a host block and NO provider
    setup, so ``resolve_pi_native_provider()`` returns ``None`` (the
    own-login path).

    :param home: The daemon HOME to populate.
    :returns: The host id written into ``config.yaml``.
    """
    omni_dir = home / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    host_name = f"e2e-multi-login-pi-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    pi_agent = home / ".pi" / "agent"
    pi_agent.mkdir(parents=True, exist_ok=True)
    (pi_agent / "auth.json").write_text(
        json.dumps(
            {
                "anthropic": {"type": "api_key", "key": "sk-e2e-fake-anthropic"},
                "openai": {"type": "api_key", "key": "sk-e2e-fake-openai"},
                "google": {"type": "api_key", "key": "e2e-fake-google"},
                "openrouter": {"type": "api_key", "key": "sk-or-e2e-fake"},
            }
        )
    )
    checked_at = int(time.time())
    (pi_agent / "models-store.json").write_text(
        json.dumps(
            {
                "anthropic": {
                    "models": _first_party_models(
                        "anthropic",
                        "https://api.anthropic.com",
                        [
                            ("claude-sonnet-4-5", "Claude Sonnet 4.5"),
                            ("claude-opus-4-6", "Claude Opus 4.6"),
                        ],
                    ),
                    "checkedAt": checked_at,
                },
                "openai": {
                    "models": _first_party_models(
                        "openai",
                        "https://api.openai.com/v1",
                        [("gpt-5.2", "GPT-5.2"), ("gpt-5.2-codex", "GPT-5.2 Codex")],
                    ),
                    "checkedAt": checked_at,
                },
                "google": {
                    "models": _first_party_models(
                        "google",
                        "https://generativelanguage.googleapis.com/v1beta",
                        [("gemini-3-pro", "Gemini 3 Pro"), ("gemini-3-flash", "Gemini 3 Flash")],
                    ),
                    "checkedAt": checked_at,
                },
                "openrouter": {
                    "models": _openrouter_models(),
                    "checkedAt": checked_at,
                },
            }
        )
    )
    # Pi's own curation mechanism: settings.json enabledModels. Pi's Ctrl+P
    # cycling honors this (interactive-mode -> resolveModelScopeFromModels);
    # the bug is that neither Omnigent picker does.
    (pi_agent / "settings.json").write_text(json.dumps({"enabledModels": [_ENABLED_MODEL]}))
    return host_id


@pytest.fixture(scope="module")
def multi_login_pi_host(
    live_server: str,
    http_client: httpx.Client,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_UnmanagedPiHost]:
    """Spawn one host daemon whose Pi is logged into several providers.

    :param live_server: Server URL the daemon registers with.
    :param http_client: HTTP client pointed at the server.
    :param tmp_path_factory: Module-scoped temp dir factory (the daemon HOME).
    :yields: The spawned host handle.
    """
    home = tmp_path_factory.mktemp("multi-login-pi-home")
    host_id = _seed_multi_login_pi_home(home)
    daemon_log = home / "host-daemon.log"
    # Pin HOME and OMNIGENT_CONFIG_HOME to the seeded dir so the daemon reads
    # the seeded (provider-less) omnigent config and Pi's multi-login, not any
    # ambient config the surrounding session exported.
    env = {
        **os.environ,
        "HOME": str(home),
        "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    # Absolute worktree roots on PYTHONPATH: the daemon-spawned runner runs
    # with cwd=<workspace>, so relative entries dangle (see the sibling
    # unmanaged-Pi module for the full rationale).
    _existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(_WORKTREE),
            str(_WORKTREE / "sdks" / "python-client"),
            str(_WORKTREE / "sdks" / "ui"),
        ]
        + ([_existing] if _existing else [])
    )
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    try:
        _wait_for_host_online(http_client, host_id, timeout=45.0)
        yield _UnmanagedPiHost(proc=proc, host_id=host_id, home=home, daemon_log=daemon_log)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _openrouter_offenders(ids: list[str]) -> list[str]:
    """Return the picker ids sourced from the multi-vendor openrouter login.

    ``enabledModels`` scopes pi to ``_ENABLED_MODEL`` alone, so under ANY
    correct curation not one openrouter row may reach a picker; each one
    present is the reported "union of every authed provider's full catalog"
    leak.

    :param ids: Qualified ``provider/model`` picker option ids.
    :returns: The offending openrouter-sourced ids.
    """
    return [model_id for model_id in ids if model_id.startswith("openrouter/")]


def test_prelaunch_model_options_honor_pis_enabled_models(
    multi_login_pi_host: _UnmanagedPiHost,
    http_client: httpx.Client,
) -> None:
    """Facet B: the pre-launch catalog must respect pi's ``enabledModels``.

    The Configure-Pi dialog's model picker is fed by
    ``GET /v1/hosts/{id}/harnesses/pi-native/model-options``
    (``pi_own_login_model_options()`` on the own-login path). With pi logged
    into four providers but curated to ONE model, the catalog must offer that
    model and must NOT dump openrouter's whole multi-vendor catalog into the
    picker. The buggy build returns the union of every ``auth.json``
    provider's full ``models-store.json`` catalog (150 options here, 144 of
    them openrouter's).

    :param multi_login_pi_host: The spawned multi-login host.
    :param http_client: HTTP client pointed at the server.
    """
    resp = http_client.get(
        f"/v1/hosts/{multi_login_pi_host.host_id}/harnesses/pi-native/model-options",
        timeout=30.0,
    )
    assert resp.status_code == 200, f"model-options failed: {resp.status_code} {resp.text}"
    ids = [option["id"] for option in resp.json().get("models", [])]
    assert _ENABLED_MODEL in ids, (
        "pre-launch pi-native model-options no longer offers the host's own "
        f"enabled Pi model {_ENABLED_MODEL!r} — a correct fix scopes the "
        f"catalog, not empties it. Got: {ids[:10]}"
    )
    offenders = _openrouter_offenders(ids)
    assert not offenders, (
        f"pre-launch pi-native model-options ignores pi's enabledModels "
        f"([{_ENABLED_MODEL!r}]): it returned {len(ids)} options — the union "
        f"of every logged-in provider's full catalog — including "
        f"{len(offenders)} models from the multi-vendor openrouter login "
        f"(e.g. {offenders[:5]}). The Configure-Pi picker is unusable and "
        f"does not match what pi's own Ctrl+P picker cycles."
    )


def test_in_session_picker_honors_pis_enabled_models(
    multi_login_pi_host: _UnmanagedPiHost,
    http_client: httpx.Client,
) -> None:
    """Facet A: the in-session picker catalog must respect ``enabledModels``.

    Launch the real ``pi`` for a terminal session on the own-login path and
    wait for its extension to push the ``external_model_options`` catalog the
    web composer's Model submenu renders (``GET /v1/sessions/{id}`` →
    ``model_options``). With pi curated to ONE model, the pushed catalog must
    not be the union of every authed provider's full catalog. The buggy
    build's ``postModelOptions`` pushes ``registry.getAvailable()`` — all
    models of all four logins, openrouter's 144 included — burying the
    session's model under hundreds of rows pi itself would never cycle.

    :param multi_login_pi_host: The spawned multi-login host.
    :param http_client: HTTP client pointed at the server.
    """
    host = multi_login_pi_host
    spec_yaml = "\n".join(
        [
            "name: pi-native-ui",
            "prompt: |",
            "  Pi is running in the session terminal.",
            "executor:",
            "  harness: pi-native",
            f"  model: {_ENABLED_MODEL}",
            "spawn: true",
            "os_env:",
            "  type: caller_process",
            "  cwd: .",
            "  sandbox:",
            "    type: none",
            "",
        ]
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = spec_yaml.encode()
        info = tarfile.TarInfo("pi-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    workspace = host.home / "ws"
    workspace.mkdir(exist_ok=True)
    create = http_client.post(
        "/v1/sessions",
        data={
            "metadata": json.dumps(
                {
                    "host_id": host.host_id,
                    "workspace": str(workspace),
                    "labels": {
                        "omnigent.ui": "terminal",
                        "omnigent.wrapper": "pi-native-ui",
                    },
                }
            )
        },
        files={"bundle": ("pi-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=60.0,
    )
    assert create.status_code in (200, 201), f"session create failed: {create.text}"
    session_id = str(create.json()["session_id"])
    marker = _bridge_marker(session_id)

    options: list[dict[str, object]] = []
    deadline = time.monotonic() + 150.0
    try:
        # Wait for the real Pi to boot and its extension to push the catalog.
        while time.monotonic() < deadline:
            if host.proc.poll() is not None:
                raise AssertionError(
                    f"host daemon exited (rc={host.proc.returncode}) before the "
                    f"picker populated; log tail:\n{host.daemon_log.read_text()[-2000:]}"
                )
            snapshot = http_client.get(f"/v1/sessions/{session_id}", timeout=30.0)
            if snapshot.status_code == 200:
                options = snapshot.json().get("model_options") or []
                if options:
                    break
            time.sleep(POLL_INTERVAL_S)

        assert options, (
            "the running Pi's extension never pushed external_model_options "
            f"for session {session_id!r} — the in-session picker stayed empty; "
            f"daemon log tail:\n{host.daemon_log.read_text()[-2000:]}"
        )
        ids = [str(option["id"]) for option in options]
        assert _ENABLED_MODEL in ids, (
            f"the in-session picker no longer offers the session's own enabled "
            f"model {_ENABLED_MODEL!r} — a correct fix scopes the catalog, not "
            f"empties it. Got {len(ids)} options: {ids[:10]}"
        )
        offenders = _openrouter_offenders(ids)
        assert not offenders, (
            f"the in-session Model picker ignores pi's enabledModels "
            f"([{_ENABLED_MODEL!r}]): the extension pushed {len(ids)} options — "
            f"the union of every logged-in provider's full catalog — including "
            f"{len(offenders)} models from the multi-vendor openrouter login "
            f"(e.g. {offenders[:5]}). The composer's Model submenu lists "
            f"hundreds of rows pi's own Ctrl+P picker would never cycle, with "
            f"the session's model buried among them."
        )
    finally:
        _kill_pi_processes(marker)
