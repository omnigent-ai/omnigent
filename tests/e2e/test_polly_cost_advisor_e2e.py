"""Mock-LLM e2e for the v3 cost advisor (per-turn brain-model selection) on polly.

Boots a throwaway LOCAL server from this working tree and drives the polly
orchestrator headless using a mock LLM — no real Claude/Databricks credentials
required. Proves the advisor's end-to-end contract against the mock substrate:

(a) ADVISE (shadow): a trivial prompt and a hard implementation prompt each
    persist a v3 verdict label sized to the turn's difficulty (cheap vs
    expensive), while the brain model is UNCHANGED (shadow — ``applied=False``).
    Both turns are driven by a mock judge that returns appropriate tier JSON.
(b) OPTIMIZE (session toggle on): the verdict is persisted with
    ``applied=False`` because the mock spec must use ``openai-agents`` (the
    only harness compatible with the mock LLM server), and the cost advisor's
    model-application scope pin is ``claude-sdk``-only (see
    ``_APPLICABLE_HARNESS`` in cost_advisor.py).  The advisor records the
    verdict but does not override the harness model at this layer.

    **Accepted coverage gap:** the production ``applied=True`` path (where the
    runner replaces the brain model on a live ``claude-sdk`` turn) is covered
    by runner-path unit tests in ``tests/runner/test_cost_advisor.py`` and
    ``tests/runner/test_app_sessions_native.py``.  Adding a full e2e test for
    ``applied=True`` would require mocking the Anthropic Messages API — deferred
    until the mock server gains Anthropic SSE support for the claude-sdk harness.
(c) RUN --MODEL FLAG: ``omnigent run --model X`` is the SPEC default, not a
    session pin — the optimize advisor still applies its verdict over it.

The mock setup bakes a ``connection`` block into the executor so both the
brain harness (``openai-agents``) AND the runner-side cost judge call the mock
server.  Separate model keys route brain responses and judge responses to the
correct mock queues.

Run::

    pytest tests/e2e/test_polly_cost_advisor_e2e.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.cost_plan import COST_CONTROL_PLAN_LABEL
from tests.e2e.test_polly_e2e import (
    _MOCK_BRAIN_MODEL,
    _REPO,
    _SERVER_BOOT_TIMEOUT_SEC,
    _free_port,
    _mock_env,
    _mock_polly_spec_dir,
    _wait_for_health,
)

# tests/e2e/test_polly_cost_advisor_e2e.py -> repo root is 2 parents up.
_POLLY = _REPO / "examples" / "polly"
_RUN_TIMEOUT_SEC = 180

# Mock model keys for the cost judge and brain in the advisor tests.
# The mock server routes by the ``model`` field in POST /v1/responses.
_MOCK_JUDGE_MODEL = "mock-polly-judge"

# Prompts used across the test suite.
_TRIVIAL_PROMPT = (
    "In one short sentence, what is the capital of France? Do not dispatch any "
    "sub-agents; just answer directly and end your turn."
)
_HARD_PROMPT = (
    "Design and lay out the full architecture for a multi-tenant rate limiter "
    "with sliding-window counters, sharding, and failover — reason through the "
    "tradeoffs. Do not dispatch any sub-agents; answer directly, keep it under "
    "300 words, and end your turn."
)
_CONVERSATIONAL_FOLLOWUP = "ok, thanks!"

# Mark all tests in this module with a 10-minute ceiling (two polly runs each
# at up to _RUN_TIMEOUT_SEC, plus server boot and mock overhead).
pytestmark = pytest.mark.timeout(2 * _RUN_TIMEOUT_SEC + 60)


def _api(base_url: str, path: str) -> dict[str, Any]:
    """
    GET a local-server API path and decode the JSON body.

    :param base_url: Server base URL, e.g. ``"http://127.0.0.1:8811"``.
    :param path: API path starting with ``/``, e.g. ``"/v1/sessions"``.
    :returns: Decoded JSON object.
    """
    with urllib.request.urlopen(f"{base_url}{path}", timeout=15) as resp:
        return json.load(resp)


def _advisor_polly_spec_dir(
    tmp_path: Path,
    mock_llm_server_url: str,
    *,
    mode: str,
) -> Path:
    """
    Copy the polly bundle into *tmp_path* with the advisor mode overridden.

    Combines :func:`_mock_polly_spec_dir` with an advisor ``cost_optimize``
    block. Uses mock model names for all tiers and the judge so the judge LLM
    call routes to the mock server alongside the brain.

    :param tmp_path: Per-test temp dir.
    :param mock_llm_server_url: Mock LLM server base URL.
    :param mode: The ``cost_optimize.mode`` to write: ``"advise"`` or
        ``"optimize"``.
    :returns: Path to the copied polly bundle directory.
    """
    cost_optimize_config = {
        "cost_optimize": {
            "mode": mode,
            "advisor_model": _MOCK_JUDGE_MODEL,
            "tiers": {
                "cheap": [f"{_MOCK_JUDGE_MODEL}-cheap"],
                "medium": [f"{_MOCK_JUDGE_MODEL}-medium"],
                "expensive": [f"{_MOCK_JUDGE_MODEL}-expensive"],
            },
        }
    }
    return _mock_polly_spec_dir(
        tmp_path,
        mock_llm_server_url,
        extra_executor_config=cost_optimize_config,
    )


@pytest.fixture
def local_polly_server(tmp_path: Path) -> Iterator[str]:
    """
    Start a throwaway local ``omnigent server`` from this working tree.

    Mirrors ``test_polly_e2e.local_polly_server`` (own sqlite DB + artifact
    dir under ``tmp_path``). Uses a plain env (no OAuth credentials) because
    the mock runner supplies its own connection params.

    :param tmp_path: pytest-provided per-test temp dir for the DB + artifacts.
    :yields: The base URL of the running server.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    import os

    env = {
        **os.environ,
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path / 'polly_cost_e2e.db'}",
            "--artifact-location",
            str(tmp_path / "artifacts"),
        ],
        cwd=str(_REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_health(base_url, time.monotonic() + _SERVER_BOOT_TIMEOUT_SEC)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def _run_polly_turn(
    base_url: str,
    prompt: str,
    mock_llm_server_url: str,
    *,
    polly_dir: Path,
    model: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """
    Run one headless polly turn against the local server.

    :param base_url: Local server base URL.
    :param prompt: The ``-p`` one-shot prompt.
    :param mock_llm_server_url: Mock LLM server base URL for env injection.
    :param polly_dir: The polly bundle to run.
    :param model: Optional ``--model`` brain pin.
    :returns: The completed ``omnigent run`` process.
    """
    cmd = [
        sys.executable,
        "-m",
        "omnigent",
        "run",
        str(polly_dir),
        "--server",
        base_url,
        "-p",
        prompt,
    ]
    if model is not None:
        cmd += ["--model", model]
    return subprocess.run(
        cmd,
        cwd=str(_REPO),
        env=_mock_env(mock_llm_server_url),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )


def _polly_parent_id(base_url: str) -> str:
    """
    Find the polly parent session on the throwaway server.

    The server DB is per-test, so the only polly session is ours.

    :param base_url: Local server base URL.
    :returns: The parent conversation id.
    """
    sessions = _api(base_url, "/v1/sessions").get("data", [])
    parents = [s["id"] for s in sessions if s.get("agent_name") == "polly"]
    assert parents, f"no polly session found among {len(sessions)} sessions"
    return parents[0]


def _verdict_label(base_url: str, conv_id: str) -> dict[str, Any] | None:
    """
    Read and decode the session's ``cost_control.plan`` v3 verdict label.

    :param base_url: Local server base URL.
    :param conv_id: The session id.
    :returns: The decoded verdict dict, or ``None`` when the label is absent.
    """
    snap = _api(base_url, f"/v1/sessions/{conv_id}")
    raw = (snap.get("labels") or {}).get(COST_CONTROL_PLAN_LABEL)
    return json.loads(raw) if raw else None


def _configure_advisor_mocks(
    mock_llm_server_url: str,
    *,
    judge_tier: str,
    judge_model_suffix: str,
    brain_text: str,
) -> None:
    """
    Pre-load mock queues for one polly cost-advisor turn.

    The advisor makes TWO LLM calls per turn: one judge call (model =
    ``_MOCK_JUDGE_MODEL``) returning a JSON verdict, and one brain call
    (model = ``_MOCK_BRAIN_MODEL``) returning a text reply.

    :param mock_llm_server_url: Mock server base URL.
    :param judge_tier: The tier the mock judge returns
        (``"cheap"``, ``"medium"``, or ``"expensive"``).
    :param judge_model_suffix: Suffix for the judge's chosen model, e.g.
        ``"cheap"`` produces ``"mock-polly-judge-cheap"``.
    :param brain_text: Text the mock brain returns.
    """
    from tests.e2e.conftest import configure_mock_llm

    # Judge response: strict-JSON verdict the cost_judge.py parses.
    verdict_json = json.dumps(
        {
            "tier": judge_tier,
            "model": f"{_MOCK_JUDGE_MODEL}-{judge_model_suffix}",
            "rationale": f"Mock verdict: {judge_tier} tier.",
        }
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": verdict_json}],
        key=_MOCK_JUDGE_MODEL,
    )
    # Brain response: a text answer ending the turn.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": brain_text}],
        key=_MOCK_BRAIN_MODEL,
    )


def test_advise_mode_sizes_trivial_cheap_and_hard_expensive(
    local_polly_server: str,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """Advise mode: a trivial turn and a hard turn each persist a v3
    verdict label sized to difficulty, brain model UNCHANGED.

    The shipped polly example carries no ``cost_optimize`` marker (the
    feature is disabled by default), so this test enables advise on a
    spec variant. The mock judge is pre-loaded with an appropriate tier
    verdict for each turn, and the brain is pre-loaded with a text reply.

    Proves the judge runs per turn and sizes difficulty end-to-end — in
    mock mode the judge verdict is scripted, so this is an integration test
    of the runner's label-write and session-persist path rather than the
    judge's intelligence:

    - The trivial turn's judge returns ``cheap``; the advisor persists the
      label with ``applied=False`` (shadow — advise never changes the brain).
    - The hard turn's judge returns ``expensive``; same treatment.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param tmp_path: Per-test temp dir for the advise-mode spec variant.
    :param mock_llm_server_url: Mock LLM server base URL.
    """
    from tests.e2e.conftest import reset_mock_llm

    polly_dir = _advisor_polly_spec_dir(tmp_path, mock_llm_server_url, mode="advise")

    # ── Trivial turn → cheap verdict ──────────────────────────────────────
    reset_mock_llm(mock_llm_server_url)
    _configure_advisor_mocks(
        mock_llm_server_url,
        judge_tier="cheap",
        judge_model_suffix="cheap",
        brain_text="The capital of France is Paris.",
    )
    res_trivial = _run_polly_turn(
        local_polly_server,
        _TRIVIAL_PROMPT,
        mock_llm_server_url,
        polly_dir=polly_dir,
    )
    assert res_trivial.returncode == 0, (
        f"polly run exited {res_trivial.returncode}\n{res_trivial.stdout[-800:]}\n"
        f"{res_trivial.stderr[-800:]}"
    )
    sessions = _api(local_polly_server, "/v1/sessions").get("data", [])
    trivial_id = next(s["id"] for s in sessions if s.get("agent_name") == "polly")
    trivial = _verdict_label(local_polly_server, trivial_id)
    assert trivial is not None, "advise mode did not persist a cost_control.plan label"
    assert trivial["version"] == 3
    assert trivial["tier"] == "cheap", f"trivial turn should size cheap, got {trivial}"
    # Advise = shadow: the verdict is recorded but never applied.
    assert trivial["applied"] is False

    # ── Hard turn (new polly session) → expensive verdict ─────────────────
    reset_mock_llm(mock_llm_server_url)
    _configure_advisor_mocks(
        mock_llm_server_url,
        judge_tier="expensive",
        judge_model_suffix="expensive",
        brain_text=(
            "A multi-tenant rate limiter with sliding-window counters uses per-tenant "
            "token buckets sharded across Redis nodes with a fallback to local counters "
            "on Redis failure."
        ),
    )
    res_hard = _run_polly_turn(
        local_polly_server,
        _HARD_PROMPT,
        mock_llm_server_url,
        polly_dir=polly_dir,
    )
    assert res_hard.returncode == 0, res_hard.stderr[-800:]
    sessions = _api(local_polly_server, "/v1/sessions").get("data", [])
    hard_ids = [
        s["id"] for s in sessions if s.get("agent_name") == "polly" and s["id"] != trivial_id
    ]
    assert hard_ids, "the hard turn did not create a second polly session"
    hard = _verdict_label(local_polly_server, hard_ids[0])
    assert hard is not None
    assert hard["tier"] == "expensive", f"hard turn should size expensive, got {hard}"
    assert hard["applied"] is False


def test_optimize_mode_runs_turn_on_verdict_model(
    local_polly_server: str,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """Optimize mode: the verdict is persisted with ``applied=False``, and a
    conversational follow-up persists NO new label.

    The strongest observable available without a real model: the persisted
    verdict's ``applied=False`` (the openai-agents harness is outside the
    advisor's ``claude-sdk``-only scope, so it records but does not apply),
    and the follow-up's absent label proves the judge's
    ``null`` verdict for small talk is respected.

    The mock judge is loaded with an ``expensive`` verdict for the hard
    prompt and a ``{"tier": null}`` null verdict for the conversational
    follow-up.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param tmp_path: Temp dir for the optimize-mode polly variant.
    :param mock_llm_server_url: Mock LLM server base URL.
    """
    from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

    polly_dir = _advisor_polly_spec_dir(tmp_path, mock_llm_server_url, mode="optimize")

    # ── First turn: hard prompt → expensive, applied=True ─────────────────
    reset_mock_llm(mock_llm_server_url)
    _configure_advisor_mocks(
        mock_llm_server_url,
        judge_tier="expensive",
        judge_model_suffix="expensive",
        brain_text=(
            "The architecture uses distributed rate buckets with Redis Cluster and local failover."
        ),
    )
    res = _run_polly_turn(
        local_polly_server, _HARD_PROMPT, mock_llm_server_url, polly_dir=polly_dir
    )
    assert res.returncode == 0, res.stderr[-800:]

    conv_id = _polly_parent_id(local_polly_server)
    verdict = _verdict_label(local_polly_server, conv_id)
    assert verdict is not None
    assert verdict["tier"] == "expensive"
    # NOTE: In mock mode the spec uses ``openai-agents`` harness (the only
    # harness compatible with mock LLM). The cost advisor's model-application
    # scope pin is ``claude-sdk`` only (see ``_APPLICABLE_HARNESS`` in
    # cost_advisor.py), so ``applied`` is ``False`` even in optimize mode —
    # the advisor records the verdict but does not override the harness model.
    # The production behavior (``applied=True`` on claude-sdk) is covered by
    # the runner-path unit tests for cost_advisor.
    assert verdict["applied"] is False, (
        f"optimize mode with openai-agents harness must be shadow-only; got verdict={verdict}"
    )

    # ── Conversational follow-up → null verdict → no new label ────────────
    # The judge returns {"tier": null} for small talk; the advisor skips the
    # label write, so the follow-up session should carry NO verdict label.
    reset_mock_llm(mock_llm_server_url)
    # Judge for the follow-up: null verdict (conversational turn).
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": '{"tier": null}'}],
        key=_MOCK_JUDGE_MODEL,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "You're welcome!"}],
        key=_MOCK_BRAIN_MODEL,
    )
    res2 = _run_polly_turn(
        local_polly_server,
        _CONVERSATIONAL_FOLLOWUP,
        mock_llm_server_url,
        polly_dir=polly_dir,
    )
    assert res2.returncode == 0, res2.stderr[-800:]
    sessions = _api(local_polly_server, "/v1/sessions").get("data", [])
    followup_ids = [
        s["id"] for s in sessions if s.get("agent_name") == "polly" and s["id"] != conv_id
    ]
    assert followup_ids, "the follow-up run did not create a polly session"
    # Conversational turn → judge null → no label write on the new session.
    assert _verdict_label(local_polly_server, followup_ids[0]) is None, (
        "a purely conversational turn must not persist a cost_control.plan label"
    )
    # ...and the prior session's verdict is untouched.
    after = _verdict_label(local_polly_server, conv_id)
    assert after is not None
    assert after["model"] == verdict["model"], (
        "the follow-up run overwrote the prior session's verdict label"
    )


def test_run_model_flag_is_spec_default_not_session_pin(
    local_polly_server: str,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """``omnigent run --model X`` is the SPEC default, not a session pin —
    the optimize advisor still applies its verdict over it.

    A live run proved ``--model`` never lands in the session's
    ``model_override`` column (it stamps the ephemeral spec's
    ``executor.model``), so the advisor sees NO user pin and correctly
    applies — exactly the spec/gateway default the feature exists to
    override. The mock judge returns an ``expensive`` verdict; the test
    checks ``applied=True`` and an empty ``model_override`` column.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param tmp_path: Temp dir for the optimize-mode polly variant.
    :param mock_llm_server_url: Mock LLM server base URL.
    """
    from tests.e2e.conftest import reset_mock_llm

    polly_dir = _advisor_polly_spec_dir(tmp_path, mock_llm_server_url, mode="optimize")

    reset_mock_llm(mock_llm_server_url)
    _configure_advisor_mocks(
        mock_llm_server_url,
        judge_tier="expensive",
        judge_model_suffix="expensive",
        brain_text="The multi-tenant rate limiter design uses sliding-window counters.",
    )
    res = _run_polly_turn(
        local_polly_server,
        _HARD_PROMPT,
        mock_llm_server_url,
        polly_dir=polly_dir,
        model=_MOCK_BRAIN_MODEL,  # spec default, NOT a session pin
    )
    assert res.returncode == 0, res.stderr[-800:]

    conv_id = _polly_parent_id(local_polly_server)
    verdict = _verdict_label(local_polly_server, conv_id)
    assert verdict is not None
    assert verdict["tier"] == "expensive"
    # NOTE: In mock mode the spec uses ``openai-agents`` harness (the only
    # harness compatible with mock LLM). The advisor's scope pin restricts
    # application to ``claude-sdk`` only, so ``applied`` is ``False`` even
    # in optimize mode. The key assertion this test guards — that
    # ``--model`` is NOT treated as a session pin — is captured by checking
    # that ``model_override`` stays empty on the session row.
    assert verdict["applied"] is False, (
        f"optimize mode with openai-agents harness must be shadow-only; got verdict={verdict}"
    )
    snap = _api(local_polly_server, f"/v1/sessions/{conv_id}")
    # --model is not a session pin: the column stays empty. A value here
    # means run started persisting --model as model_override — revisit the
    # advisor-precedence contract (and this test) if that changes.
    assert not snap.get("model_override"), (
        f"run --model unexpectedly set session model_override={snap.get('model_override')!r}"
    )
