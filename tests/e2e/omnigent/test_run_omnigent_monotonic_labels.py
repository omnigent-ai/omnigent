"""
End-to-end test for the ``monotonic`` label-schema constraint
under ``omnigent run``.

Drives a real LLM-backed agent through the full omnigent
stack (CLI → bundle materialization → spec parse → policy
engine build → workflow agent loop → policy enforcement →
``conversation_labels`` persistence) and asserts that a
write violating the declared monotonic direction is silently
dropped end to end.

**The scenario:**

The YAML declares ``integrity`` as a decreasing-monotonic
label with ``values: ["0", "1"]`` and ``initial: "1"``. Two
label policies:

- ``taint_on_input`` fires on every INPUT phase and writes
  ``integrity: "0"`` (a decrease — allowed).
- ``try_untaint_on_output`` fires on every OUTPUT phase and
  writes ``integrity: "1"`` (an increase — must be dropped).

After one turn, the persisted ``conversation_labels`` row
must read ``integrity = "0"``: the taint write landed, the
untaint write was rejected by the schema-check inside
``apply_label_writes``.

If ``integrity`` ends up as ``"1"`` (or absent), the
monotonic constraint silently failed somewhere on the real
agent path — exactly the regression kasey_uhlenhuth's bug
report (#6) calls out as undertested.

**What breaks if this fails:**

- ``omnigent.runtime.policies.engine.PolicyEngine.apply_label_writes``
  stops calling ``_filter_schema_valid``, or
  ``_filter_schema_valid`` stops applying the monotonic
  branch — every monotonic constraint in production silently
  no-ops.
- ``build_policy_engine`` regresses on ``initial_labels``
  loading — the engine starts each turn with an empty hot
  cache, so the monotonic check sees ``current=None`` (which
  is always allowed as a seed) and the rogue write lands.
- The omnigent → omnigent label-schema translation
  regresses (``min``/``max`` → ``decreasing``/``increasing``),
  ``LabelDef.monotonic`` becomes ``None``, and the constraint
  is dropped at parse time.
- The workflow's OUTPUT enforcement site stops calling the
  policy engine entirely (the engine wouldn't even see the
  attempted write).

Uses the same fixture pattern as
``test_run_omnigent_resumption.py`` and
``test_run_omnigent_instructions.py``: ``openai-agents`` honors
``OPENAI_BASE_URL`` directly so the test needs no
``~/.databrickscfg`` patching.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from tests.e2e.omnigent.conftest import configure_mock_llm

# Same harness as the other run_omnigent e2e tests — the
# invariant under test is on the policy + label-store path,
# not on harness specifics, and openai-agents has the most
# reliable ``-p`` one-shot dispatch.
_HARNESS = "openai-agents"
# Mock model name — the mock LLM server uses the "default" queue
# for any model string.
_MODEL = "mock-model"

# 180 s matches the resumption suite's headroom for DBOS
# sqlite migrations + cold imports + one openai-agents turn.
_RUN_TIMEOUT_SEC = 180


def _isolated_home_env(base_env: dict[str, str], home: Path) -> dict[str, str]:
    """
    Override ``HOME`` so the subprocess writes its persistent
    chat.db inside the test's temp dir.

    Mirrors the resumption suite's isolation pattern. Without
    this the test would write to the developer's real
    ``~/.omnigent/chat.db`` and the post-run label query
    could pick up unrelated state (or vice-versa).

    :param base_env: The fixture-provided env dict.
    :param home: Per-test tmp_path acting as the fake HOME.
    :returns: A fresh dict suitable for ``subprocess.run(env=)``.
    """
    env = dict(base_env)
    env["HOME"] = str(home)
    return env


def test_monotonic_decreasing_constraint_enforced_end_to_end(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    Run an agent whose YAML declares a decreasing-monotonic
    ``integrity`` label, plus two function policies that race
    each other across phases:

    - INPUT: write ``"0"`` (a decrease — allowed).
    - OUTPUT: write ``"1"`` (an increase — must be dropped).

    After the turn completes, the persisted
    ``conversation_labels`` row must show ``integrity = "0"``.

    What breaks if this fails:
      - The decreasing-monotonic constraint silently no-ops
        and the OUTPUT write lands ("1" persisted).
      - The omnigent → omnigent label-schema translation
        loses ``monotonic: min`` somewhere ("1" persisted via
        the same observable failure).
      - ``build_policy_engine`` doesn't load existing labels
        into the per-turn hot cache, so the monotonic check
        sees ``current=None`` and the rogue write is allowed
        (also "1" persisted).
    """
    configure_mock_llm(mock_llm_server_url, [{"text": "ok"}])
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = _isolated_home_env(mock_credentials_env, fake_home)

    # The agent YAML. Omnigent-flavored format (no
    # ``spec_version``); the omnigent-compat adapter
    # translates labels/label_schema/policies into the native
    # Omnigent guardrails shape at load time.
    agent_dir = tmp_path / "monotonic_agent"
    agent_dir.mkdir()
    yaml_path = agent_dir / "agent.yaml"
    yaml_path.write_text(
        """\
name: monotonic-e2e
prompt: |
  You will receive any prompt. Reply with exactly the word
  "ok" and nothing else.
labels:
  integrity: "1"
label_schema:
  integrity:
    values: ["0", "1"]
    # ``min`` translates to ``decreasing`` on the Omnigent side —
    # writes must move towards the lower-indexed value, never
    # towards the higher. Once a label hits ``"0"`` it cannot
    # be raised back to ``"1"`` for the rest of the
    # conversation.
    monotonic: min
policies:
  taint_on_input:
    type: function
    on: [request]
    function:
      path: omnigent.policies.function.make_fixed_action_callable
      arguments:
        action: allow
        set_labels: {integrity: "0"}
        on_phases: [request]
    set_labels: [integrity]
  try_untaint_on_output:
    type: function
    on: [response]
    function:
      path: omnigent.policies.function.make_fixed_action_callable
      arguments:
        action: allow
        set_labels: {integrity: "1"}
        on_phases: [response]
    set_labels: [integrity]
"""
    )

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--model",
            _MODEL,
            "--harness",
            _HARNESS,
            "-p",
            "say something",
            "--no-log",
        ],
        env=env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert result.returncode == 0, (
        f"omnigent run exited {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # The persistent store the subprocess writes lives at
    # ``$HOME/.omnigent/chat.db`` — see
    # ``omnigent.chat._omnigent_persistent_dir``. With HOME pointed
    # at our temp dir, that's where the labels we want to
    # inspect ended up.
    persistent_db = fake_home / ".omnigent" / "chat.db"
    assert persistent_db.is_file(), (
        f"Persistent store was not created at {persistent_db}. "
        f"The subprocess didn't reach the workflow stage."
    )

    # Read the persisted label row directly. We pick the most
    # recent conversation (there's only one in this test, but
    # be explicit so a future change that adds extra system
    # conversations doesn't accidentally hit a stale row).
    with sqlite3.connect(str(persistent_db)) as conn:
        conv_row = conn.execute(
            "SELECT id FROM conversations "
            "WHERE kind = 'default' "
            "ORDER BY updated_at DESC, id DESC "
            "LIMIT 1"
        ).fetchone()
        assert conv_row is not None, (
            "No conversation found in chat.db — the workflow "
            "didn't persist anything. stdout=" + repr(result.stdout)
        )
        conv_id = conv_row[0]
        label_rows = conn.execute(
            "SELECT key, value FROM conversation_labels WHERE conversation_id = ? ",
            (conv_id,),
        ).fetchall()

    labels = dict(label_rows)
    # Load-bearing assertion: the OUTPUT-phase write to ``"1"``
    # was rejected by the monotonic constraint, so the final
    # persisted value reflects the INPUT-phase write only.
    integrity = labels.get("integrity")
    assert integrity == "0", (
        f"Monotonic constraint regression: integrity should be "
        f"'0' (taint write from INPUT, untaint write from OUTPUT "
        f"dropped by ``monotonic: min``), got {integrity!r}.\n"
        f"  - 'integrity' missing → labels never persisted at all "
        f"(workflow didn't reach the policy enforcement site).\n"
        f"  - 'integrity' == '1' → monotonic constraint not "
        f"enforced; the rogue OUTPUT write landed.\n"
        f"All labels for conversation {conv_id!r}: {labels!r}\n"
        f"stderr tail: {result.stderr[-2000:]}"
    )


def test_monotonic_increasing_picks_most_restrictive_in_one_evaluation(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    Two function policies fire on the SAME phase (INPUT), both
    writing the same monotonic-increasing key. The most
    restrictive (highest-index) value should be the final
    persisted state — not whichever policy happens to come
    last in YAML order.

    The semantics of ``monotonic: max`` is "labels move only
    upwards." A policy that writes ``"2"`` must not be
    silently overridden by a later policy in the same
    evaluation call that writes ``"1"`` — that's the policy
    author's invariant ("once tainted high, stay high").

    YAML order under test:

    1. ``taint_high`` writes ``integrity: "2"``.
    2. ``taint_low`` writes ``integrity: "1"``.

    Initial value: ``"0"``. Both writes pass an isolated
    monotonic check (``"2" >= "0"`` and ``"1" >= "0"``), but
    composing them via accumulator overwrite produces the
    LESS restrictive value. Correct semantics: the final
    state must be ``"2"``.

    What breaks if this fails (i.e. final == "1"):
      - ``PolicyEngine.evaluate``'s
        ``accumulated.update(filtered_labels)`` uses
        last-write-wins semantics on the in-flight
        accumulator. The monotonic-direction-aware merge that
        would honor "max takes precedence" for
        ``monotonic: max`` is missing.
      - From a policy author's perspective: writing two
        policies that both raise the same taint label is a
        data race in YAML order — one silently nullifies the
        other depending on file ordering.
    """
    configure_mock_llm(mock_llm_server_url, [{"text": "ok"}])
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = _isolated_home_env(mock_credentials_env, fake_home)

    agent_dir = tmp_path / "monotonic_increasing_agent"
    agent_dir.mkdir()
    yaml_path = agent_dir / "agent.yaml"
    yaml_path.write_text(
        """\
name: monotonic-increasing-e2e
prompt: |
  You will receive any prompt. Reply with exactly the word
  "ok" and nothing else.
labels:
  integrity: "0"
label_schema:
  integrity:
    values: ["0", "1", "2"]
    # ``max`` translates to ``increasing`` on the Omnigent side —
    # labels can only move towards higher-indexed values.
    monotonic: max
policies:
  taint_high:
    type: function
    on: [request]
    function:
      path: omnigent.policies.function.make_fixed_action_callable
      arguments:
        action: allow
        set_labels: {integrity: "2"}
        on_phases: [request]
    set_labels: [integrity]
  taint_low:
    type: function
    on: [request]
    function:
      path: omnigent.policies.function.make_fixed_action_callable
      arguments:
        action: allow
        set_labels: {integrity: "1"}
        on_phases: [request]
    set_labels: [integrity]
"""
    )

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--model",
            _MODEL,
            "--harness",
            _HARNESS,
            "-p",
            "say something",
            "--no-log",
        ],
        env=env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert result.returncode == 0, (
        f"omnigent run exited {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    persistent_db = fake_home / ".omnigent" / "chat.db"
    with sqlite3.connect(str(persistent_db)) as conn:
        conv_row = conn.execute(
            "SELECT id FROM conversations "
            "WHERE kind = 'default' "
            "ORDER BY updated_at DESC, id DESC "
            "LIMIT 1"
        ).fetchone()
        assert conv_row is not None
        conv_id = conv_row[0]
        label_rows = conn.execute(
            "SELECT key, value FROM conversation_labels WHERE conversation_id = ?",
            (conv_id,),
        ).fetchall()

    labels = dict(label_rows)
    integrity = labels.get("integrity")
    # The load-bearing claim: the "max" direction's
    # composition must keep the highest-index value when
    # multiple in-evaluation writes target the same key.
    # Without that, accumulator-last-write-wins lets a "lower"
    # write silently nullify a "higher" predecessor — which
    # is the kasey_uhlenhuth observation that the monotonic
    # constraint "isn't enforced" for the multi-policy case.
    assert integrity == "2", (
        f"Multi-policy monotonic accumulation regression: "
        f"integrity should be '2' (max of 2 from taint_high "
        f"and 1 from taint_low under monotonic=increasing), "
        f"got {integrity!r}.\n"
        f"  - 'integrity' == '1' → accumulator used "
        f"last-write-wins; the lower taint_low write silently "
        f"overwrote the taint_high '2'. The constraint's "
        f"'labels only move upwards' invariant is violated by "
        f"YAML ordering rather than by an explicit author "
        f"choice.\n"
        f"All labels for conversation {conv_id!r}: {labels!r}\n"
        f"stderr tail: {result.stderr[-2000:]}"
    )
