"""E2E: a wiped agent-cache entry must not brick a session.

Journey (all user-observable):

1. Create a session on a bundled agent and exchange a turn — works.
2. The server's on-disk agent cache entry loses its ``config.yaml``
   (what a tmp cleaner does to ``/tmp``-hosted caches; the bundle
   tarball in the ArtifactStore stays intact).
3. The server restarts (routine deploy/restart — empties the in-memory
   spec tier, so the next spec load hits the poisoned disk tier).
4. The user reopens the session and sends another message.

Expected: the server re-materializes the spec from the ArtifactStore
(the source of truth still holds the bundle) and the turn completes.

Observed (bug): ``AgentCache.load`` treats ``workdir.is_dir()`` as a
disk-cache hit and ``load_spec`` raises ``FileNotFoundError:
config.yaml not found in <cache dir>``, which escapes as an unhandled
500 from every session request that resolves the spec — the session is
permanently bricked even though the bundle is still in the store.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_bundled_session,
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    set_fallback_mock_llm,
)

_BEFORE_PROMPT = "Hello before the cache wipe."
_BEFORE_REPLY = "Reply before the cache wipe."
_AFTER_PROMPT = "Hello after the server restart."
_AFTER_REPLY = "Reply after the server restart."

_AGENT_YAML = """\
spec_version: 1
name: cache_wipe_probe
prompt: |
  Reply with one short sentence.

executor:
  model: {model}
  config:
    harness: openai-agents
  auth:
    type: api_key
    api_key: mock-key
    base_url: {mock_base_url}
"""


def _server_tmp_dir() -> Path:
    """Root temp dir of the spawned server (holds test.db / artifacts / server.log)."""
    db_uri = str(_server_state["database_uri"])
    return Path(db_uri.removeprefix("sqlite:///")).parent


def _agent_cache_dir_for(marker: str) -> Path:
    """Find the server's extracted agent-cache dir whose config.yaml carries *marker*.

    The AgentCache extracts each agent bundle to
    ``<artifact-location>/.cache/<agent_id>/`` (see ``omnigent/cli.py``:
    ``AgentCache(cache_dir=Path(art_loc) / ".cache")``). The bundled test
    agent's model name is unique per run, so it identifies the entry.
    """
    cache_root = _server_tmp_dir() / "artifacts" / ".cache"
    matches = [
        entry
        for entry in sorted(cache_root.iterdir())
        if entry.is_dir()
        and (entry / "config.yaml").is_file()
        and marker in (entry / "config.yaml").read_text(encoding="utf-8")
    ]
    assert len(matches) == 1, (
        f"expected exactly one agent-cache entry for marker {marker!r} under "
        f"{cache_root}, found {[str(m) for m in matches]!r}"
    )
    return matches[0]


def _config_not_found_log_lines() -> list[str]:
    """Server-log lines showing the unhandled config.yaml spec-load failure.

    The fixture's ``server.log`` captures the process stdout/stderr, whose
    boot banner names the *real* structured log file (``  log: <path>``) —
    one per spawn, so a restarted server adds a second path. Search them all.
    """
    banner_path = _server_tmp_dir() / "server.log"
    if not banner_path.exists():
        return []
    banner_text = banner_path.read_text(encoding="utf-8", errors="replace")
    log_paths = [banner_path] + [
        Path(m.group(1)) for m in re.finditer(r"^\s*log:\s+(\S+)$", banner_text, re.M)
    ]
    lines: list[str] = []
    for log_path in log_paths:
        if not log_path.exists():
            continue
        lines.extend(
            line
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if "config.yaml not found" in line
        )
    return lines


def _probe_message_post(base_url: str, session_id: str) -> str:
    """POST a user message event directly; return ``status_code: body`` evidence."""
    try:
        resp = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "probe after restart"}],
                },
            },
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        return f"transport error: {exc!r}"
    return f"{resp.status_code}: {resp.text[:500]}"


def _probe_terminal_create(base_url: str, session_id: str) -> str:
    """POST a terminal create; the route loads the agent spec inline, so a
    poisoned cache entry escapes as the ticket's unhandled 500 here."""
    try:
        resp = httpx.post(
            f"{base_url}/v1/sessions/{session_id}/resources/terminals",
            json={"terminal": "sh", "session_key": "cache-wipe-probe"},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        return f"transport error: {exc!r}"
    return f"{resp.status_code}: {resp.text[:500]}"


@pytest.mark.timeout(300)
def test_session_survives_wiped_agent_cache_after_restart(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A session must keep serving requests after its disk cache entry is wiped.

    The agent bundle is still in the ArtifactStore (source of truth); losing
    the extracted ``config.yaml`` from the disk cache plus a server restart
    must not turn every session request into an unhandled 500.
    """
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    restart = _server_state.get("restart_server")
    if not callable(restart):
        pytest.skip("requires the locally spawned restartable server")
    restart_server = cast("Callable[[], None]", restart)
    runner_id = str(_server_state["runner_id"])

    model = f"cache-wipe-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _BEFORE_REPLY}, {"text": _AFTER_REPLY}],
        key=model,
    )
    set_fallback_mock_llm(mock_llm_server_url, model, _AFTER_REPLY)

    session_id = _create_bundled_session(
        live_server,
        runner_id,
        _AGENT_YAML.format(model=model, mock_base_url=f"{mock_llm_server_url}/v1"),
    )
    try:
        # 1. The session works: one full turn round-trips.
        page.goto(f"{live_server}/c/{session_id}")
        composer = page.get_by_placeholder("Send a message…")
        expect(composer).to_be_visible(timeout=30_000)
        composer.fill(_BEFORE_PROMPT)
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.get_by_text(_BEFORE_REPLY, exact=True).first).to_be_visible(timeout=120_000)

        # 2. The extracted cache entry loses its config.yaml (tmp-cleaner
        #    fault); the bundle tarball in the ArtifactStore is untouched.
        agent_cache_dir = _agent_cache_dir_for(model)
        (agent_cache_dir / "config.yaml").unlink()

        # 3. Routine server restart: the in-memory spec tier starts empty,
        #    so the next spec load hits the poisoned disk tier.
        restart_server()

        # 4. The user comes back to the session and sends another message.
        #    The server must recover the spec from the ArtifactStore and
        #    complete the turn instead of 500ing every session request.
        #    Right after a restart the SPA can still be re-establishing its
        #    session stream, and a Send click in that window may never POST;
        #    retry the send. The bug under test denies every send, so
        #    retries cannot mask it.
        try:
            page.reload()
            composer = page.get_by_placeholder("Send a message…")
            expect(composer).to_be_visible(timeout=30_000)
            reply = page.get_by_text(_AFTER_REPLY, exact=True).first
            send_error: AssertionError | None = None
            for _attempt in range(3):
                composer.fill(_AFTER_PROMPT)
                page.get_by_role("button", name="Send", exact=True).click()
                try:
                    expect(reply).to_be_visible(timeout=30_000)
                    send_error = None
                    break
                except AssertionError as exc:
                    send_error = exc
            if send_error is not None:
                raise send_error
        except AssertionError as exc:
            message_probe = _probe_message_post(live_server, session_id)
            terminal_probe = _probe_terminal_create(live_server, session_id)
            log_lines = _config_not_found_log_lines()
            raise AssertionError(
                "session stopped serving requests after its agent-cache entry "
                "was wiped and the server restarted; the server should have "
                "re-materialized the spec from the ArtifactStore instead of "
                "failing. Direct message POST after restart returned "
                f"{message_probe}. Terminal create POST returned "
                f"{terminal_probe}. Spec-load errors in the server log: "
                f"{log_lines!r}"
            ) from exc
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)
