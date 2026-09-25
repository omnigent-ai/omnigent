"""E2E: both web Pi model pickers honor Pi's own ``enabledModels`` curation.

On the own-login path -- a host where Omnigent manages no Pi provider so
``resolve_pi_native_provider()`` returns ``None`` -- Pi runs on its own
``~/.pi/agent`` login. When that login spans several providers (here
anthropic + openai + a multi-vendor OpenRouter catalog) and ``settings.json``
curates ``enabledModels`` to a single model, both web pickers must list just
that curated scope (matching pi's own Ctrl+P cycling), not the union of every
authed provider's full catalog:

* Facet 1 (in-session, surface ``web``): the resident extension posts the
  session's resolved model scope, so the composer's Model picker offers the
  curated model instead of ``registry.getAvailable()``'s multi-provider
  flood.

* Facet 2 (pre-launch, surface ``web``): the Configure-Pi dialog's Model
  picker fetches ``GET /v1/hosts/{id}/harnesses/pi-native/model-options``,
  which ``pi_own_login_model_options()`` scopes by ``enabledModels``.

Both assertions are written against the FIXED behavior: they are RED on a
build whose pickers dump the union (hundreds of rows, most of them
OpenRouter's multi-vendor catalog, the curated default buried) and GREEN once
both pickers honor the curation.

The catalog is produced by a REAL unmanaged multi-provider Pi login on a real
host daemon (facet 2 serves it straight from the daemon; facet 1 launches the
real ``pi`` CLI, whose extension pushes its live scope). Launching the real
Pi terminal needs ``pi`` / ``tmux`` / ``node`` on PATH; the module skips
cleanly when any is absent.

    .venv/bin/python -m pytest \
      tests/e2e_ui/start_session/test_pi_native_multi_provider_picker.py -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
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
from playwright.async_api import Route, async_playwright, expect

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e_ui.start_session.test_start_session import (
    _open_entry_models,
    _pi_native_agents_body,
    _run_in_fresh_loop,
)

_WORKTREE = Path(__file__).resolve().parents[3]

pytestmark = [
    pytest.mark.skipif(
        (_pi := cli_unavailable_reason("pi")) is not None,
        reason=f"pi-native multi-provider picker e2e needs a runnable 'pi' CLI; {_pi}.",
    ),
    pytest.mark.skipif(
        shutil.which("tmux") is None,
        reason="pi-native terminal launch needs 'tmux' on PATH.",
    ),
    pytest.mark.skipif(
        (_node := cli_unavailable_reason("node")) is not None,
        reason=f"pi-native extension needs 'node'; {_node}.",
    ),
]

# The single model the host's Pi is curated to via settings.json enabledModels.
# A correct picker would scope to this; the bug lists every provider's catalog.
_CURATED_MODEL = "anthropic/claude-sonnet-4-5"

# Leaked runner/zygote env would send the daemon's spawned runner down the
# zygote-fork path and hang it; strip it so the daemon starts a clean runner.
_LEAKED_RUNNER_ENV = (
    "OMNIGENT_RUNNER_ID",
    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN",
    "OMNIGENT_RUNNER_TUNNEL_TOKEN",
    "OMNIGENT_RUNNER_PARENT_PID",
    "OMNIGENT_RUNNER_ISOLATE_SESSION",
    "OMNIGENT_RUNNER_WORKSPACE",
    "OMNIGENT_HOST_ID",
    "OMNIGENT_HOST_TOKEN",
    "OMNIGENT_HOST_NAME",
    "RUNNER_SERVER_URL",
    "OMNIGENT_REMOTE_AUTH_TOKEN",
)


def _openrouter_catalog() -> list[dict[str, Any]]:
    """Build a multi-vendor OpenRouter catalog (the union's dominant bulk).

    OpenRouter is a single authed provider whose catalog spans many vendors,
    so once it is logged in "authed provider's models" stops being a usable
    scope -- the exact shape the ticket calls out.
    """
    vendors = [
        ("inception", "Mercury"),
        ("inclusionai", "Ling"),
        ("nex-agi", "Nex AGI"),
        ("mistralai", "Mistral"),
        ("z-ai", "Z.AI: GLM 5.3"),
        ("qwen", "Qwen3"),
        ("deepseek", "DeepSeek"),
        ("google", "Gemini"),
        ("meta-llama", "Llama"),
        ("cohere", "Command"),
    ]
    catalog: list[dict[str, Any]] = []
    for vendor, label in vendors:
        for index in range(15):
            base = f"{vendor}/model-{index}"
            for model_id, name in (
                (base, f"{label} {index}"),
                (f"{base}:batch", f"{label} {index} (batch)"),
            ):
                catalog.append(
                    {
                        "id": model_id,
                        "name": name,
                        "api": "openai-completions",
                        "provider": "openrouter",
                        "baseUrl": "https://openrouter.ai/api/v1",
                        "input": ["text"],
                    }
                )
    return catalog


def _seed_multi_provider_pi_home(home: Path) -> str:
    """Seed *home* with a logged-in multi-provider (unmanaged) Pi + host config.

    Writes an ``auth.json`` logged into anthropic, openai and openrouter, a
    ``models-store.json`` carrying their catalogs (OpenRouter's a large
    multi-vendor list), a ``settings.json`` curating ``enabledModels`` to one
    model, and an ``.omnigent/config.yaml`` with ONLY a host block and no
    provider setup -- the state where ``resolve_pi_native_provider()`` is
    ``None`` and Pi runs on its own login.

    :param home: The daemon HOME to populate.
    :returns: The host id written into ``config.yaml``.
    """
    omni_dir = home / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": f"e2e-multi-pi-{host_id[:12]}"}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    pi_agent = home / ".pi" / "agent"
    pi_agent.mkdir(parents=True, exist_ok=True)
    (pi_agent / "settings.json").write_text(json.dumps({"enabledModels": [_CURATED_MODEL]}))
    (pi_agent / "auth.json").write_text(
        json.dumps(
            {
                "anthropic": {"type": "api_key", "key": "sk-e2e-anthropic-fake"},
                "openai": {"type": "api_key", "key": "sk-e2e-openai-fake"},
                "openrouter": {"type": "api_key", "key": "sk-e2e-openrouter-fake"},
            }
        )
    )
    (pi_agent / "models-store.json").write_text(
        json.dumps(
            {
                "anthropic": {
                    "models": [
                        {
                            "id": "claude-sonnet-4-5",
                            "name": "Claude Sonnet 4.5",
                            "api": "anthropic-messages",
                            "provider": "anthropic",
                            "baseUrl": "https://api.anthropic.com",
                            "input": ["text", "image"],
                        },
                        {
                            "id": "claude-opus-4-1",
                            "name": "Claude Opus 4.1",
                            "api": "anthropic-messages",
                            "provider": "anthropic",
                            "baseUrl": "https://api.anthropic.com",
                            "input": ["text", "image"],
                        },
                    ],
                    "checkedAt": 1750000000,
                },
                "openai": {
                    "models": [
                        {
                            "id": "gpt-4o",
                            "name": "GPT-4o",
                            "api": "openai-responses",
                            "provider": "openai",
                            "baseUrl": "https://api.openai.com/v1",
                            "input": ["text", "image"],
                        },
                    ],
                    "checkedAt": 1750000000,
                },
                "openrouter": {"models": _openrouter_catalog(), "checkedAt": 1750000000},
            }
        )
    )
    return host_id


def _client(base_url: str) -> httpx.Client:
    """HTTP client that presents as a first-party non-browser caller."""
    return httpx.Client(
        base_url=base_url,
        timeout=300,
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )


def _wait_for_host_online(client: httpx.Client, host_id: str, timeout: float = 45.0) -> None:
    """Poll ``GET /v1/hosts`` until *host_id* is online."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = client.get("/v1/hosts")
            if resp.status_code == 200:
                for host in resp.json().get("hosts", []):
                    if host["host_id"] == host_id and host["status"] == "online":
                        return
        except httpx.ConnectError:
            pass
        time.sleep(0.25)
    raise AssertionError(f"host {host_id!r} did not appear online within {timeout}s")


class _MultiProviderPiHost:
    """A spawned host daemon whose Pi is logged into several providers."""

    def __init__(self, host_id: str, home: Path, base_url: str) -> None:
        self.host_id = host_id
        self.home = home
        self.base_url = base_url


@pytest.fixture(scope="module")
def multi_provider_pi_host(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_MultiProviderPiHost]:
    """Spawn one host daemon with a logged-in multi-provider (unmanaged) Pi.

    :param live_server: Server URL the daemon registers with.
    :param tmp_path_factory: Module-scoped temp dir factory (the daemon HOME).
    :yields: The spawned :class:`_MultiProviderPiHost`.
    """
    home = tmp_path_factory.mktemp("multi-provider-pi-home")
    host_id = _seed_multi_provider_pi_home(home)
    daemon_log = home / "host-daemon.log"
    env = {
        **os.environ,
        "HOME": str(home),
        "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
        "OMNIGENT_DATA_DIR": str(home / ".omnigent"),
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    for leaked in _LEAKED_RUNNER_ENV:
        env.pop(leaked, None)
    for zygote in [key for key in env if key.startswith("OMNIGENT_RUNNER_ZYGOTE")]:
        env.pop(zygote, None)
    # Absolute worktree roots: the runner the daemon spawns runs with
    # cwd=<workspace>, so any relative PYTHONPATH entry would resolve wrong.
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(_WORKTREE),
            str(_WORKTREE / "sdks" / "python-client"),
            str(_WORKTREE / "sdks" / "ui"),
        ]
        + ([existing] if existing else [])
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
        with _client(live_server) as client:
            _wait_for_host_online(client, host_id, timeout=45.0)
        yield _MultiProviderPiHost(host_id=host_id, home=home, base_url=live_server)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _launch_pi_session_with_pushed_catalog(host: _MultiProviderPiHost) -> str:
    """Create a real pi-native session on *host* and wait for its pushed catalog.

    Launches the real ``pi`` CLI (via the runner) whose resident extension
    posts the session's model catalog back to the server.

    :param host: The spawned multi-provider Pi host.
    :returns: The session id, once its pushed catalog has arrived.
    """
    spec_yaml = "\n".join(
        [
            "name: pi-native-ui",
            "prompt: |",
            "  Pi is running in the session terminal.",
            "executor:",
            "  harness: pi-native",
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
    with _client(host.base_url) as client:
        create = client.post(
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
        deadline = time.monotonic() + 150.0
        options: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            resp = client.get(f"/v1/sessions/{session_id}", timeout=10.0)
            if resp.status_code == 200:
                options = resp.json().get("model_options") or []
                if options:
                    break
            time.sleep(1.5)
    assert options, (
        f"pi-native session {session_id!r} never pushed a model catalog; "
        "the in-session Model picker stayed empty"
    )
    return session_id


@pytest.fixture(scope="module")
def pushed_catalog_session(multi_provider_pi_host: _MultiProviderPiHost) -> Iterator[str]:
    """A live pi-native session whose extension pushed its model catalog."""
    session_id = _launch_pi_session_with_pushed_catalog(multi_provider_pi_host)
    try:
        yield session_id
    finally:
        with _client(multi_provider_pi_host.base_url) as client:
            with contextlib.suppress(httpx.HTTPError):
                client.delete(f"/v1/sessions/{session_id}", timeout=10.0)


def test_pi_native_prelaunch_picker_lists_only_the_curated_scope(
    multi_provider_pi_host: _MultiProviderPiHost,
) -> None:
    """Facet 2 (pre-launch, web): the Configure-Pi Model picker is curated.

    The picker fetches the host's REAL
    ``/v1/hosts/{id}/harnesses/pi-native/model-options``. With Pi curated to
    one model, it must list exactly that scope, not the union of every
    logged-in provider's catalog. Only host-discovery chrome is stubbed; the
    catalog comes from the real unmanaged multi-provider Pi login.
    """
    _run_in_fresh_loop(_drive_prelaunch_picker(multi_provider_pi_host))


async def _drive_prelaunch_picker(host: _MultiProviderPiHost) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            host_id = host.host_id

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "hosts": [
                                {
                                    "host_id": host_id,
                                    "name": "e2e-multi-pi",
                                    "owner": "e2e",
                                    "status": "online",
                                    "configured_harnesses": {"pi-native": True},
                                }
                            ]
                        }
                    ),
                )

            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_pi_native_agents_body(),
                )

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), handle_agent_scan
            )
            # Deliberately DO NOT stub pi-native model-options: it passes
            # through to the real server -> the real host daemon's catalog.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ "{host_id}": ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{host.base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await _open_entry_models(page, "ag_pi_e2e")
            models_menu = page.get_by_test_id("new-chat-landing-agent-models")
            await expect(models_menu).to_be_visible()
            rows = models_menu.get_by_role("menuitemcheckbox")
            await _wait_for_rows(rows, minimum=1)

            curated = page.get_by_test_id(f"new-chat-landing-agent-model-{_CURATED_MODEL}")
            assert await curated.count() == 1, (
                f"pre-launch picker no longer offers the curated model {_CURATED_MODEL!r} "
                "-- a correct fix scopes the catalog, not empties it"
            )
            openrouter_rows = models_menu.locator(
                '[data-testid^="new-chat-landing-agent-model-openrouter/"]'
            )
            openrouter_count = await openrouter_rows.count()
            assert openrouter_count == 0, (
                "pre-launch picker ignores pi's enabledModels "
                f"([{_CURATED_MODEL!r}]): it rendered {openrouter_count} rows from "
                "the multi-vendor OpenRouter login"
            )
            count = await rows.count()
            assert count < 10, (
                f"pre-launch picker listed {count} rows -- the union of every "
                "logged-in provider's catalog -- instead of the curated scope"
            )
        finally:
            await context.close()
            await browser.close()


def test_pi_native_in_session_picker_lists_only_the_curated_scope(
    multi_provider_pi_host: _MultiProviderPiHost,
    pushed_catalog_session: str,
) -> None:
    """Facet 1 (in-session, web): the composer Model picker is curated.

    The resident extension pushes the session's resolved model scope (Pi's
    ``enabledModels``); the composer's Model picker must render that curated
    set, not every authed provider's full catalog.
    """
    _run_in_fresh_loop(
        _drive_in_session_picker(multi_provider_pi_host.base_url, pushed_catalog_session)
    )


async def _drive_in_session_picker(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(f"{base_url}/c/{session_id}")
            gear = page.get_by_test_id("composer-config-gear")
            await gear.wait_for(state="visible", timeout=45_000)
            await gear.click()
            await page.get_by_test_id("composer-agent-edit").click()
            models_menu = page.get_by_test_id("composer-agent-models")
            await expect(models_menu).to_be_visible()
            rows = models_menu.get_by_role("menuitemcheckbox")
            await _wait_for_rows(rows, minimum=1)

            curated = models_menu.locator(f'[data-model-id="{_CURATED_MODEL}"]')
            assert await curated.count() == 1, (
                f"in-session picker no longer offers the curated model {_CURATED_MODEL!r} "
                "-- a correct fix scopes the catalog, not empties it"
            )
            openrouter_rows = models_menu.locator('[data-model-id*="openrouter"]')
            openrouter_count = await openrouter_rows.count()
            assert openrouter_count == 0, (
                "in-session composer picker ignores pi's enabledModels "
                f"([{_CURATED_MODEL!r}]): it rendered {openrouter_count} rows from "
                "the multi-vendor OpenRouter login"
            )
            count = await rows.count()
            assert count < 10, (
                f"in-session picker listed {count} rows -- the union of every "
                "logged-in provider's catalog -- instead of the curated scope"
            )
        finally:
            await context.close()
            await browser.close()


async def _wait_for_rows(rows: Any, *, minimum: int, timeout_s: float = 30.0) -> None:
    """Poll until *rows* has at least *minimum* entries (the catalog rendered)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await rows.count() >= minimum:
            return
        time.sleep(0.25)
