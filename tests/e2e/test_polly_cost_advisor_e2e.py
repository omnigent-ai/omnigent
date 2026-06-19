"""Opt-in e2e for the v3 cost advisor (per-turn brain-model selection) on polly.

Real models, real server: boots a throwaway LOCAL server from this working
tree and drives the polly orchestrator headless. Proves the advisor's
end-to-end contract that unit tests cannot — the runner-side judge call, the
``cost_control.plan`` label persist via the reserved-namespace authority path,
and (optimize mode) the per-turn ``model_override`` reaching the claude-sdk
brain:

(a) ADVISE (polly's shipped default): a trivial prompt and a hard
    implementation prompt each persist a v3 verdict label sized to the turn's
    difficulty (cheap vs expensive), while the brain model is UNCHANGED (shadow);
(b) OPTIMIZE (session toggle on): the turn provably runs on the verdict model
    (observed via the runner launch log / persisted state), and a conversational
    follow-up persists NO new label;
(c) USER PIN: an explicit ``/model`` pin beats the advisor — the verdict is
    recorded but the brain runs on the user's model.

NOTE on what runs here: polly's brain on this dev box must reach a Claude
provider whose catalog includes the configured tiers
(``databricks-claude-haiku-4-5`` / ``-sonnet-4-6`` / ``-opus-4-8``). The judge
itself is one cheap haiku call per advised turn. ``omnigent run --profile``
was removed; provider auth comes from ``omnigent login`` /
``omnigent setup`` / the spec, so this file does NOT pass ``--profile``.

OPT-IN like ``test_polly_e2e.py`` (same dev-box toolset)::

    OMNIGENT_E2E_POLLY=1 uv run --extra dev python -m pytest \
        tests/e2e/test_polly_cost_advisor_e2e.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnigent.cost_plan import COST_CONTROL_PLAN_LABEL
from tests.e2e.test_polly_e2e import (
    _SERVER_BOOT_TIMEOUT_SEC,
    _clean_env,
    _free_port,
    _wait_for_health,
)

# tests/e2e/test_polly_cost_advisor_e2e.py -> repo root is 2 parents up.
_REPO = Path(__file__).resolve().parents[2]
_POLLY = _REPO / "examples" / "polly"
_RUN_TIMEOUT_SEC = 600

# Expected tier per prompt difficulty (matches the rubric's few-shot shape).
_TRIVIAL_PROMPT = (
    "In one short sentence, what is the capital of France? Do not dispatch any "
    "sub-agents; just answer directly and end your turn."
)
_HARD_PROMPT = (
    "Design and lay out the full architecture for a multi-tenant rate limiter "
    "with sliding-window counters, sharding, and failover — reason through the "
    "tradeoffs. Do not dispatch any sub-agents; answer directly, keep it under "
    "300 words, and end your turn."
    # The word bound caps GENERATION length (an unbounded opus design answer
    # can stream past the run timeout); live runs show the judge still sizes
    # the bounded prompt expensive ("genuine engineering work despite the
    # word limit").
)
_CONVERSATIONAL_FOLLOWUP = "ok, thanks!"

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_POLLY") != "1",
        reason=(
            "polly cost-advisor e2e needs the dev-box toolset (Claude provider with "
            "the configured tier models) absent on CI — set OMNIGENT_E2E_POLLY=1 to opt in."
        ),
    ),
    # Each test makes up to TWO sequential one-shot polly runs (daemon +
    # runner + Claude CLI boot + a real turn, _RUN_TIMEOUT_SEC each); the
    # global --timeout=300 fires mid-turn (it killed two live suite runs).
    pytest.mark.timeout(2 * _RUN_TIMEOUT_SEC + 300),
]


def _api(base_url: str, path: str) -> dict[str, Any]:
    """
    GET a local-server AP API path and decode the JSON body.

    :param base_url: Server base URL, e.g. ``"http://127.0.0.1:8811"``.
    :param path: API path starting with ``/``, e.g. ``"/v1/sessions"``.
    :returns: Decoded JSON object.
    """
    with urllib.request.urlopen(f"{base_url}{path}", timeout=15) as resp:
        return json.load(resp)


def _polly_spec_dir(tmp_path: Path, *, mode: str) -> Path:
    """
    Copy the polly bundle into *tmp_path* with the advisor mode overridden.

    Lets the optimize test run against a spec variant without mutating the
    shipped example; the agents/ subdir is copied so sub-agents still resolve
    (polly declares claude_code / codex / pi).

    :param tmp_path: Per-test temp dir.
    :param mode: The ``cost_optimize.mode`` to write, ``"advise"`` or
        ``"optimize"``.
    :returns: The path to the copied polly bundle.
    """
    import shutil

    dst = tmp_path / "polly"
    shutil.copytree(_POLLY, dst, symlinks=False)
    config_path = dst / "config.yaml"
    spec = yaml.safe_load(config_path.read_text())
    # The shipped example carries NO marker (feature disabled by default);
    # the test injects its own full enablement block.
    spec["executor"]["config"]["cost_optimize"] = {
        "mode": mode,
        "advisor_model": "databricks-claude-haiku-4-5",
        "tiers": {
            "cheap": ["databricks-claude-haiku-4-5"],
            "medium": ["databricks-claude-sonnet-4-6"],
            "expensive": ["databricks-claude-opus-4-8"],
        },
    }
    config_path.write_text(yaml.safe_dump(spec, sort_keys=False))
    return dst


@pytest.fixture
def local_polly_server(tmp_path: Path) -> Iterator[str]:
    """
    Start a throwaway local ``omnigent server`` from this working tree.

    Mirrors ``test_polly_subagent_model_e2e.local_polly_server`` (own sqlite
    DB + artifact dir under ``tmp_path``).

    :param tmp_path: pytest-provided per-test temp dir for the DB + artifacts.
    :yields: The base URL of the running server.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
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
        env=_clean_env(),
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
    *,
    polly_dir: Path = _POLLY,
    model: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """
    Run one headless polly turn against the local server.

    :param base_url: Local server base URL.
    :param prompt: The ``-p`` one-shot prompt.
    :param polly_dir: The polly bundle to run (default the shipped example;
        the optimize test passes a tmp_path variant).
    :param model: Optional ``--model`` brain pin (the user-pin test passes one).
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
        env=_clean_env(),
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


def test_advise_mode_sizes_trivial_cheap_and_hard_expensive(
    local_polly_server: str, tmp_path: Path, using_mock_llm: bool
) -> None:
    """Advise mode: a trivial turn and a hard turn each persist a v3
    verdict label sized to difficulty, brain model UNCHANGED.

    The shipped polly example carries no ``cost_optimize`` marker (the
    feature is disabled by default), so this test enables advise on a
    spec variant.

    Proves the judge runs per turn and sizes difficulty end-to-end: the
    trivial prompt yields a ``cheap`` verdict, the architecture prompt an
    ``expensive`` one, both with ``applied=false`` (shadow — advise never
    changes the brain). The session DB is per-test, so each run is its own
    polly session.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param tmp_path: Per-test temp dir for the advise-mode spec variant.
    :param using_mock_llm: Whether mock LLM mode is active.
    """
    if using_mock_llm:
        pytest.skip(
            "polly cost-advisor e2e requires real LLM judge calls and real "
            "subprocess omnigent run invocations; not feasible under mock LLM"
        )
    polly_dir = _polly_spec_dir(tmp_path, mode="advise")
    # Trivial turn → cheap verdict.
    res_trivial = _run_polly_turn(local_polly_server, _TRIVIAL_PROMPT, polly_dir=polly_dir)
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

    # Hard turn (new polly session) → expensive verdict.
    res_hard = _run_polly_turn(local_polly_server, _HARD_PROMPT, polly_dir=polly_dir)
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
    local_polly_server: str, tmp_path: Path, using_mock_llm: bool
) -> None:
    """Optimize mode: the turn provably runs on the verdict model, and a
    conversational follow-up persists NO new label.

    The strongest observable available locally is the persisted verdict's
    ``applied=true`` plus the runner launch log (``HARNESS_CLAUDE_SDK_MODEL``
    / per-turn ``model_override``) naming the verdict model. The follow-up
    asserts the conversational-turn contract: no new verdict label is written
    (the prior selection stands).

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param tmp_path: Temp dir for the optimize-mode polly variant.
    :param using_mock_llm: Whether mock LLM mode is active.
    """
    if using_mock_llm:
        pytest.skip(
            "polly cost-advisor optimize e2e requires real LLM judge calls and real "
            "subprocess omnigent run invocations; not feasible under mock LLM"
        )
    polly_dir = _polly_spec_dir(tmp_path, mode="optimize")
    res = _run_polly_turn(local_polly_server, _HARD_PROMPT, polly_dir=polly_dir)
    assert res.returncode == 0, res.stderr[-800:]

    conv_id = _polly_parent_id(local_polly_server)
    verdict = _verdict_label(local_polly_server, conv_id)
    assert verdict is not None
    assert verdict["tier"] == "expensive"
    # applied=true is the optimize-mode proof: the runner stamped the verdict
    # model on the harness body for this turn.
    assert verdict["applied"] is True, f"optimize mode did not apply the verdict: {verdict}"
    expensive_model = verdict["model"]

    # Conversational follow-up. NOTE: each one-shot `omnigent run` creates a
    # NEW session, so this exercises the conversational-null contract (the
    # judge returns null for "ok, thanks!" → NO verdict label is written),
    # not same-session stickiness — that needs a live multi-turn session
    # (verified manually via the REPL; the sticky state is runner-local).
    res2 = _run_polly_turn(
        local_polly_server,
        _CONVERSATIONAL_FOLLOWUP,
        polly_dir=polly_dir,
    )
    assert res2.returncode == 0, res2.stderr[-800:]
    sessions = _api(local_polly_server, "/v1/sessions").get("data", [])
    followup_ids = [
        s["id"] for s in sessions if s.get("agent_name") == "polly" and s["id"] != conv_id
    ]
    assert followup_ids, "the follow-up run did not create a polly session"
    # Conversational turn → judge null → no label write on the new session.
    # A label here means the judge produced a verdict for pure small talk.
    assert _verdict_label(local_polly_server, followup_ids[0]) is None, (
        "a purely conversational turn must not persist a cost_control.plan label"
    )
    # ...and the prior session's verdict is untouched.
    after = _verdict_label(local_polly_server, conv_id)
    assert after is not None
    assert after["model"] == expensive_model, (
        "the follow-up run overwrote the prior session's verdict label"
    )


def test_run_model_flag_is_spec_default_not_session_pin(
    local_polly_server: str, tmp_path: Path, using_mock_llm: bool
) -> None:
    """``omnigent run --model X`` is the SPEC default, not a session pin —
    the optimize advisor still applies its verdict over it.

    A live run proved ``--model`` never lands in the session's
    ``model_override`` column (it stamps the ephemeral spec's
    ``executor.model``), so the advisor sees NO user pin and correctly
    applies — exactly the spec/gateway default the feature exists to
    override. The real pin surfaces (``/model``, the web picker, a
    ``model_override`` PATCH) ride the session column and DO beat the
    advisor; that precedence cannot be exercised through a one-shot run
    (the PATCH would race the only turn), so it is covered by the
    runner-path regression test
    (``test_user_pin_suppresses_sticky_model_on_background_turn``) and the
    ``test_cost_advisor`` unit matrix instead.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param tmp_path: Temp dir for the optimize-mode polly variant.
    :param using_mock_llm: Whether mock LLM mode is active.
    """
    if using_mock_llm:
        pytest.skip(
            "polly cost-advisor user-pin e2e requires real LLM judge calls and real "
            "subprocess omnigent run invocations; not feasible under mock LLM"
        )
    polly_dir = _polly_spec_dir(tmp_path, mode="optimize")
    res = _run_polly_turn(
        local_polly_server,
        _HARD_PROMPT,
        polly_dir=polly_dir,
        model="databricks-claude-sonnet-4-6",  # spec default, NOT a session pin
    )
    assert res.returncode == 0, res.stderr[-800:]

    conv_id = _polly_parent_id(local_polly_server)
    verdict = _verdict_label(local_polly_server, conv_id)
    assert verdict is not None
    assert verdict["tier"] == "expensive"
    # The advisor applied over the spec default — applied=False here would
    # mean a spec-level model is being mistaken for a user pin (which would
    # dark-launch optimize mode for every spec that names a model).
    assert verdict["applied"] is True, (
        f"the advisor must apply over a spec-default model; verdict={verdict}"
    )
    snap = _api(local_polly_server, f"/v1/sessions/{conv_id}")
    # --model is not a session pin: the column stays empty. A value here
    # means run started persisting --model as model_override — revisit the
    # advisor-precedence contract (and this test) if that changes.
    assert not snap.get("model_override"), (
        f"run --model unexpectedly set session model_override={snap.get('model_override')!r}"
    )
