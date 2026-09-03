"""UI journey: ``skills: none`` must not block the agent's own bundled skill.

A claude-sdk agent whose bundle ships ``skills/<name>/SKILL.md`` and sets
``skills: none`` (the hermetic host-skill filter) must still be able to
invoke its own bundled skill through the CLI's native ``Skill`` tool.
``skills: none`` exists to keep the *host's* ``~/.claude/skills`` and
``<cwd>/.claude/skills`` out of the session — the bundle's own skills ride
``--plugin-dir`` and are promised to remain usable (see
``_resolve_skills_option`` in ``omnigent/inner/claude_sdk_executor.py``).

Journey driven here, on the real web SPA against a live server + runner
and the real claude CLI pointed at the mock Anthropic endpoint:

1. bundle a claude-sdk agent with ``skills: none`` plus its own
   ``skills/<name>/SKILL.md``, and start a session on it
2. ask the agent to run that bundled skill
3. the model calls the native ``Skill`` tool with the bundled skill's name
4. observable failure: the tool call is rejected with
   ``<tool_use_error>Skill <name> is not in this session's skills
   allowlist</tool_use_error>`` — the agent's own skill is visible but
   not invokable, and the model carries on without it

Regression guard: the final assertion (the ``Skill`` tool invocation of
the bundled skill succeeds — no allowlist rejection) fails whenever the
executor stops seeding bundled skills into the SDK's otherwise-empty
``skills=[]`` allowlist under ``skills: none``.
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from typing import Any

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    reset_mock_llm,
)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

# Unforgeable skill name: the unique suffix cannot be hallucinated, so any
# occurrence in the captured LLM traffic is genuinely this test's skill.
_SKILL_NAME = "probe-brew-tea-x7k3f9"
_SKILL_MARKER = "BREW_TEA_MARKER_x7k3f9"
_DONE_TEXT = "SKILL_ATTEMPT_FINISHED"


def _build_bundle(name: str, mock_llm_base_url: str) -> bytes:
    """Build a strict-format claude-sdk bundle with one bundled skill.

    Uses the strict ``config.yaml`` (spec_version 1) format so the server's
    parser discovers ``skills/<dir>/SKILL.md`` into ``agent_spec.skills`` —
    the exact shape the bug report describes (bundle ships the skill,
    ``skills: none`` filters host skills). ``executor.auth`` routes the
    claude CLI's ``ANTHROPIC_BASE_URL`` at the mock server.

    :param name: Agent name (unique per test run).
    :param mock_llm_base_url: Mock server base URL WITHOUT ``/v1`` (the
        Anthropic SDK appends ``/v1/messages`` itself).
    :returns: The ``.tar.gz`` bundle bytes for multipart upload.
    """
    config = {
        "spec_version": 1,
        "name": name,
        "prompt": "You are a tester agent. Do exactly what the user asks.",
        "skills": "none",
        "executor": {
            "type": "omnigent",
            "model": "claude-sonnet-4-20250514",
            "config": {"harness": "claude-sdk"},
            "auth": {
                "type": "api_key",
                "api_key": "mock-key",
                "base_url": mock_llm_base_url,
            },
        },
    }
    skill_md = (
        f"---\nname: {_SKILL_NAME}\ndescription: Bundled probe skill for the "
        f"skills-none regression test.\n---\n\n# Probe skill\n\n"
        f"When invoked output the literal string {_SKILL_MARKER}.\n"
    )
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.safe_dump(config, sort_keys=False).encode()
            info = tarfile.TarInfo("config.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
            md = skill_md.encode()
            info = tarfile.TarInfo(f"skills/{_SKILL_NAME}/SKILL.md")
            info.size = len(md)
            tar.addfile(info, io.BytesIO(md))
        return buf.getvalue()


def _create_session(base_url: str, runner_id: str, mock_llm_server_url: str) -> str:
    """Upload the bundle, create the session, and bind it to the runner.

    :param base_url: Live server base URL.
    :param runner_id: Token-bound runner id to PATCH-bind.
    :param mock_llm_server_url: Mock server base URL (no ``/v1``).
    :returns: The new session id.
    """
    name = f"skills-none-probe-{uuid.uuid4().hex[:8]}"
    bundle = _build_bundle(name, mock_llm_server_url)
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _skill_tool_results(reqs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract the tool_result blocks answering native ``Skill`` tool_use calls.

    Walks every captured Anthropic ``/v1/messages`` request: maps assistant
    ``tool_use`` blocks named ``Skill`` (with this test's skill as input) to
    their ids, then collects the user-role ``tool_result`` blocks that
    reference those ids. The CLI resends the cumulative conversation each
    call, so duplicates are deduped by ``tool_use_id``.

    :param reqs: Captured request bodies from ``GET /mock/requests``.
    :returns: One tool_result dict per distinct Skill invocation.
    """
    skill_use_ids: set[str] = set()
    results: dict[str, dict[str, Any]] = {}
    for req in reqs:
        for msg in req.get("messages") or []:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if (
                    msg.get("role") == "assistant"
                    and block.get("type") == "tool_use"
                    and block.get("name") == "Skill"
                    and (block.get("input") or {}).get("skill") == _SKILL_NAME
                ):
                    skill_use_ids.add(str(block.get("id")))
                if (
                    msg.get("role") == "user"
                    and block.get("type") == "tool_result"
                    and str(block.get("tool_use_id")) in skill_use_ids
                ):
                    results[str(block.get("tool_use_id"))] = block
    return list(results.values())


@pytest.mark.timeout(600)
def test_skills_none_allows_invoking_bundled_skill(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Native ``Skill`` invocation of the bundle's own skill must succeed.

    The mock scripts the model to call the ``Skill`` tool on the bundled
    skill; the claude CLI executes the call and sends the tool_result back
    on its next API call, where this test captures it. On the current build
    that result is ``<tool_use_error>Skill <name> is not in this session's
    skills allowlist</tool_use_error>`` — the reported bug.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        reset_mock_llm(mock_llm_server_url)
        session_id = _create_session(live_server, runner_id, mock_llm_server_url)
        try:
            token = f"skillsnone-{uuid.uuid4().hex[:6]}"
            # Three identical Skill tool_call entries, not one: the CLI
            # spawns side API calls at turn start (session title, etc.)
            # that route to the same match-queue and can consume the first
            # entry — extra Skill invocations are harmless (each yields its
            # own tool_result; the assertion dedupes by tool_use_id).
            configure_mock_llm(
                mock_llm_server_url,
                [
                    {
                        "tool_calls": [
                            {
                                "call_id": f"toolu_{uuid.uuid4().hex[:10]}",
                                "name": "Skill",
                                "arguments": json.dumps({"skill": _SKILL_NAME}),
                            }
                        ]
                    }
                ]
                * 3
                + [{"text": _DONE_TEXT}] * 10,
                match=token,
            )

            page.goto(f"{live_server}/c/{session_id}")

            _send(page, f"Run your bundled skill {_SKILL_NAME} now. {token}")

            # Turn settles: the scripted final text lands and the working
            # indicator clears.
            expect(page.locator(_ASSISTANT).filter(has_text=_DONE_TEXT).first).to_be_visible(
                timeout=240_000
            )
            expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

            # ── The reproduction assertion ──────────────────────────────
            # The CLI echoed the Skill call's tool_result back to the model;
            # read it off the mock's captured requests.
            reqs_resp = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0)
            reqs_resp.raise_for_status()
            captured = reqs_resp.json()
            reqs = captured.get("requests", []) if isinstance(captured, dict) else captured

            results = _skill_tool_results(reqs)
            assert results, (
                "the model's native Skill tool_use for the bundled skill never "
                "produced a tool_result in the captured LLM traffic — the turn "
                "did not exercise the Skill tool"
            )
            rejected = [
                r
                for r in results
                if r.get("is_error") or "skills allowlist" in json.dumps(r, default=str)
            ]
            assert not rejected, (
                f"skills: none blocked the agent's own bundled skill: the native "
                f"Skill tool invocation of {_SKILL_NAME!r} was rejected with "
                f"{json.dumps(rejected[0], default=str)[:500]} — bundled skills "
                f"(loaded via --plugin-dir) must remain invokable when the spec "
                f"only filters HOST skills"
            )
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:  # best-effort teardown
                respawned.kill()
                respawned.wait(timeout=5)
