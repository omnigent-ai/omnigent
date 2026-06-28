"""Per-harness live characterization test — antigravity (Gemini) SDK harness.

Runs ``omnigent run <spec> --harness antigravity ...`` as a real subprocess and
asserts structural invariants over the persisted **conversation transcript**
(not stdout): a non-empty, non-error assistant reply; a pinned + default Gemini
model both complete; multi-turn history is retained across a ``--continue``
resume (the #278 plain-text history seeding); and the happy path exits 0 with a
non-error transcript. This is the end-to-end gate for the antigravity harness:
the full path from CLI parse -> spec materialize -> ``_build_antigravity_spawn_env``
threading ``HARNESS_ANTIGRAVITY_*`` -> the ``antigravity`` harness wrap ->
:class:`AntigravityExecutor` driving the in-process ``google.antigravity`` SDK
(``conversation.send`` -> ``receive_steps()`` -> mapped ``ExecutorEvent``\\ s) ->
``TurnComplete`` -> the conversation store.

Unlike the other per-harness e2e tests (claude-sdk / codex / openai-agents / pi),
the antigravity harness is **Gemini-native**: the SDK has no OpenAI-compatible
``base_url`` and there is deliberately no Databricks-gateway path, so this test
does NOT use ``patched_databrickscfg`` / ``omnigent_credentials_env``'s gateway
URL — it authenticates purely from the configured / ambient Gemini key. This
mirrors :mod:`tests.e2e.omnigent.test_per_harness_cursor` (the other
backend-native SDK harness): because a Gemini key is not provisioned on CI, the
test **skips** (rather than fails) when no key is present, so the e2e shards stay
green; it runs for real wherever a key is configured.

**Why this test cannot use the mock LLM server:** The ``google-antigravity``
SDK has no OpenAI-compatible ``base_url`` and no Databricks-gateway path.
Setting ``OPENAI_BASE_URL`` to the mock server has no effect on this harness —
the SDK always connects directly to Google's Gemini backend using the Gemini
API key. There is no intercept point equivalent to ``OPENAI_BASE_URL`` in the
Gemini SDK, so the mock-LLM approach used by other harness tests (e.g.
``test_per_harness_openai_agents.py``) cannot be applied here. The
``pytest.skip`` in :func:`_antigravity_skip_reason` gates each test cleanly
when the SDK or key is absent.

**Prerequisites (skipped cleanly when absent):**
- ``google.antigravity`` importable in the Omnigent venv (the ``antigravity``
  extra — ``pip install 'omnigent[antigravity]'``).
- A Gemini / Antigravity API key configured (a stored ``antigravity:`` config
  block resolvable via
  :func:`omnigent.onboarding.antigravity_auth.antigravity_api_key_configured`,
  or an ambient ``GEMINI_API_KEY`` / ``ANTIGRAVITY_API_KEY``). The SDK *requires*
  a key — there is no login flow.

**glibc / dev-shim caveat:** the SDK spawns a bundled native ``localharness``
binary linked against a recent glibc (needs ``GLIBC_ABI_DT_RELR``), so a live
turn only succeeds on hosts with **glibc >= ~2.36**. On an older dev box the turn
fails at setup with ``RuntimeError: ... localharness: ... version
'GLIBC_ABI_DT_RELR' not found`` unless ``ANTIGRAVITY_HARNESS_PATH`` points at a
loader-shim that runs the untouched bundled binary through a newer glibc's
loader. This gate intentionally does NOT probe glibc (CI runners are modern); it
is documented here so a glibc-2.31 dev run that fails the live turn is
recognized as the host, not a harness regression.

**What breaks if this fails (with prerequisites present):**
- :class:`AntigravityExecutor` regresses (the ``Step`` -> ``ExecutorEvent``
  translation, the ``custom_tools`` bridge, per-session agent reuse, the
  ``LocalAgentConfig`` field filtering, or the Gemini-native auth threading).
- ``_build_antigravity_spawn_env`` stops resolving the configured / ambient
  Gemini key into ``HARNESS_ANTIGRAVITY_API_KEY`` (#277 regression), or stops
  threading ``--model`` into ``HARNESS_ANTIGRAVITY_MODEL`` (#276 regression).
- The ``google-antigravity`` SDK contract changes (``Agent`` / ``Conversation``
  / ``receive_steps`` shape).
- ``--continue`` stops seeding prior-turn history onto a fresh antigravity
  session (#278 regression — the SDK has no history-injection API, so prior
  turns are replayed as a plain-text ``"Conversation so far: ..."`` prefix).
- ``omnigent.cli``'s ``run`` one-shot / interactive paths stop persisting the
  assistant reply, or harness dispatch for ``antigravity`` regresses.

**Scope note (stable-on-main behaviors only):** this file is authored off
``main`` (#194 + #276/#277/#278). It deliberately does NOT assert tool-parameter
schemas (#279), policy enforcement (#284), the model catalog (#290), or error
normalization (#297) — those land with their own PRs and are tested there.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from omnigent.entities.conversation import MessageData

_HARNESS = "antigravity"

# A valid Gemini id the harness pins for the model-selection test. ``gemini-3-pro``
# 404s on a plain AI-Studio key, so the suite stays on the flash tier:
# ``gemini-2.5-flash`` is the explicit pin; the harness default is
# ``gemini-3.5-flash`` (resolved by ``AntigravityExecutor`` when no model is set).
_PINNED_MODEL = "gemini-2.5-flash"
_DEFAULT_MODEL = "gemini-3.5-flash"

# Minimum assistant-text length. A reply longer than ~10 chars proves the turn
# produced a genuine model response (not an empty turn or a one-token stub).
_MIN_ASSISTANT_CHARS = 10

# Substrings that betray an error item / failed turn rather than a real reply.
# The antigravity executor surfaces failures as an ``ExecutorError`` whose
# message is prefixed "Antigravity ... failed"; a glibc / model / egress failure
# lands here. Matched case-insensitively against the joined transcript text.
_ERROR_MARKERS: tuple[str, ...] = (
    "antigravity turn failed",
    "antigravity agent setup failed",
    "glibc_abi_dt_relr",
    "traceback (most recent call last)",
)

# Subprocess timeout per ``omnigent run`` invocation. The antigravity SDK boots
# a native subprocess and round-trips to the Gemini backend, so cold turns take
# ~10-60s; 200s keeps headroom on a contended CI host without letting a hung run
# pin the suite forever.
_RUN_TIMEOUT_SEC = 200
_TOOL_SCHEMA_PRODUCT = "443242686"
_TOOL_SCHEMA_RESULT_MARKER = f"schema-tool-result={_TOOL_SCHEMA_PRODUCT}"

# Minimal antigravity-native agent spec. Single-file legacy form (``name`` /
# ``prompt`` / ``executor``) — the form ``omnigent run <file>`` accepts without a
# spec directory. No ``executor.auth`` block: the key resolves from the stored
# ``antigravity:`` config / ambient ``GEMINI_API_KEY`` via
# ``_build_antigravity_spawn_env``. The model is pinned per-test via ``--model``
# (an empty ``executor`` would default to the harness's ``gemini-3.5-flash``).
_AGENT_YAML = """\
name: antigravity_per_harness_e2e
prompt: |
  You are a terse test assistant. Answer in as few words as possible, but
  always answer in a complete sentence of at least a few words.

executor:
  harness: antigravity
"""


def _antigravity_skip_reason(omnigent_python: Path) -> str | None:
    """Return a skip reason when the antigravity prerequisites are absent.

    Mirrors the cursor harness gate: the antigravity harness talks only to
    Google's Gemini backend (no Databricks-gateway path), and CI does not
    provision a Gemini key, so an absent prerequisite is a clean **skip** rather
    than a failure — keeping the e2e shards green while the test runs for real
    wherever a key is configured.

    Both prerequisites are probed in the *Omnigent venv* interpreter (the one the
    subprocess uses), not the current pytest interpreter, because the test shells
    out: the SDK import and the key-config resolution must hold *there*.

    :param omnigent_python: Interpreter the ``omnigent`` subprocess will use.
    :returns: A human-readable skip reason, or ``None`` when both the
        ``google.antigravity`` SDK and a usable Gemini key are present.
    """
    probe = subprocess.run(
        [
            str(omnigent_python),
            "-c",
            # Booleans only — never print the key. ``antigravity_api_key_configured``
            # resolves a stored ``antigravity:`` block; the ambient env vars are the
            # SDK's direct fallback (and what ``_build_antigravity_spawn_env`` adopts).
            "import importlib.util, os, sys;"
            "have_sdk = importlib.util.find_spec('google.antigravity') is not None;"
            "from omnigent.onboarding.antigravity_auth import "
            "antigravity_api_key_configured as cfg, ANTIGRAVITY_ENV_VARS;"
            "have_key = cfg() or any(os.environ.get(v) for v in ANTIGRAVITY_ENV_VARS);"
            "sys.stdout.write(f'{int(have_sdk)}{int(have_key)}')",
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        # The probe itself failed to import the onboarding module — treat as a
        # missing/installation-broken prerequisite and skip with the detail.
        return (
            "antigravity prerequisite probe failed in the Omnigent venv "
            f"(exit {probe.returncode}): {probe.stderr.strip()[:400]!r}"
        )
    flags = probe.stdout.strip()
    have_sdk = flags[:1] == "1"
    have_key = flags[1:2] == "1"
    if not have_sdk:
        return (
            "antigravity prerequisite missing: the 'google.antigravity' SDK is "
            "not importable in the Omnigent venv (install the 'antigravity' "
            "extra: pip install 'omnigent[antigravity]')."
        )
    if not have_key:
        return (
            "antigravity prerequisite missing: no Gemini API key configured. The "
            "Antigravity SDK requires a key (no login flow); configure an "
            "'antigravity:' block via 'omni setup' or export GEMINI_API_KEY / "
            "ANTIGRAVITY_API_KEY. Skipped (not failed) because CI does not "
            "provision a Gemini key — this Gemini-native harness has no "
            "Databricks-gateway fallback."
        )
    return None


@pytest.fixture
def antigravity_spec(tmp_path: Path) -> Path:
    """Materialize the minimal antigravity agent spec and return its path.

    :param tmp_path: Per-test temp dir.
    :returns: Path to the written ``agent.yaml``.
    """
    spec_path = tmp_path / "agent.yaml"
    spec_path.write_text(_AGENT_YAML, encoding="utf-8")
    return spec_path


@pytest.fixture
def antigravity_tool_schema_spec(tmp_path: Path) -> Path:
    """Materialize an antigravity spec with a structured-argument function tool."""
    spec_path = tmp_path / "agent_with_calculate_tool.yaml"
    spec_path.write_text(
        """\
name: antigravity_tool_schema_e2e
prompt: |
  You have a calculate tool. When the user asks for arithmetic, use the tool.
  The calculate tool requires exactly one argument named expression.

executor:
  harness: antigravity

tools:
  calculate:
    type: function
    description: |
      Evaluate a basic arithmetic expression. Pass exactly one argument named
      expression containing the expression string.
    callable: tests.resources.examples._shared.tool_functions.calculate
""",
        encoding="utf-8",
    )
    return spec_path


def _antigravity_env(base_env: dict[str, str], home: Path) -> dict[str, str]:
    """Build the subprocess env for an antigravity run.

    Starts from the shared ``omnigent_credentials_env`` (so PATH, the onboarding
    suppression knobs, and the worktree ``PYTHONPATH`` propagate) but isolates
    ``$HOME`` and the Omnigent state/config roots into the test's temp dir, so
    the persistent conversation store this test reads (``$HOME/.omnigent/chat.db``)
    is private and the run never threads onto an unrelated prior conversation.

    The gateway-oriented ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` keys inherited
    from the base env are irrelevant to this harness (the SDK has no
    ``base_url``); the Gemini key is resolved by ``_build_antigravity_spawn_env``
    from the configured ``antigravity:`` block / ambient env, which survives the
    ``HOME`` swap because the ambient ``GEMINI_API_KEY`` (when set) is carried in
    ``base_env`` and the keychain is process-global.

    :param base_env: The ``omnigent_credentials_env`` fixture dict.
    :param home: Per-test fake ``$HOME``.
    :returns: A fresh env dict for ``subprocess.run(env=...)``.
    """
    env = dict(base_env)
    env["HOME"] = str(home)
    env["OMNIGENT_CONFIG_HOME"] = str(home / ".omnigent")
    env["OMNIGENT_DATA_DIR"] = str(home / ".omnigent")
    return env


def _assistant_transcript_texts(db_path: Path) -> list[str]:
    """Return every assistant message text block from the persistent store.

    Reads the conversation transcript directly from the SQLite store the
    subprocess wrote, rather than scraping the CLI's stdout — the transcript is
    the durable record of what the harness actually produced. Mirrors
    ``_conversation_texts`` in
    :mod:`tests.e2e.omnigent.test_server_remote_omnigent_autonomous_flows`, but
    filtered to assistant-authored messages so the assertions can't be satisfied
    by the echoed user prompt.

    :param db_path: Path to ``$HOME/.omnigent/chat.db``.
    :returns: Assistant message texts across every conversation in the store.
    """
    # Lazy import: the conversation store pulls in SQLAlchemy, and keeping it out
    # of module import time means a skipped test (no SDK / key) never pays for it.
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    store = SqlAlchemyConversationStore(f"sqlite:///{db_path}")
    texts: list[str] = []
    convs = store.list_conversations(limit=50)
    for conv in convs.data:
        page = store.list_items(conversation_id=conv.id, limit=500)
        for item in page.data:
            if item.type != "message" or not isinstance(item.data, MessageData):
                continue
            if item.data.role != "assistant":
                continue
            for block in item.data.content or []:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, str):
                    texts.append(text)
    return texts


def _run_one_shot(
    *,
    omnigent_python: Path,
    omnigent_repo_root: Path,
    spec_path: Path,
    env: dict[str, str],
    prompt: str,
    model: str | None,
) -> subprocess.CompletedProcess[str]:
    """Run a one-shot ``omnigent run <spec> --harness antigravity -p <prompt>``.

    Session-backed (no ``--no-session``) so the turn is persisted to
    ``$HOME/.omnigent/chat.db`` for transcript inspection and so a later
    ``--continue`` can thread onto it.

    :param omnigent_python: Interpreter from the ``omnigent_python`` fixture.
    :param omnigent_repo_root: Cwd for the subprocess (repo root on sys.path).
    :param spec_path: Path to the antigravity agent YAML.
    :param env: Subprocess env (HOME-isolated, Gemini-keyed).
    :param prompt: The ``-p`` prompt for this turn.
    :param model: Gemini model to pin via ``--model``; ``None`` uses the harness
        default (``gemini-3.5-flash``).
    :returns: The completed process (stdout/stderr captured for diagnostics).
    """
    argv = [
        str(omnigent_python),
        "-m",
        "omnigent",
        "run",
        str(spec_path),
        "--harness",
        _HARNESS,
    ]
    if model is not None:
        argv += ["--model", model]
    argv += ["-p", prompt, "--no-log"]
    return subprocess.run(
        argv,
        env=env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )


def _assert_clean_assistant_reply(
    db_path: Path,
    result: subprocess.CompletedProcess[str],
    *,
    label: str,
) -> str:
    """Assert the run exited 0 and persisted a non-empty, non-error reply.

    The single shared post-condition for the happy-path turns: graceful exit,
    plus a transcript whose assistant text is long enough to be a real reply and
    free of the executor's error markers (a failed antigravity turn surfaces as
    a ``failed`` session + an error item, never a silent empty success).

    :param db_path: The session store the run wrote.
    :param result: The completed ``omnigent run`` process.
    :param label: Short label for failure messages, e.g. ``"smoke"``.
    :returns: The joined assistant transcript text (for callers that assert more).
    """
    assert result.returncode == 0, (
        f"antigravity {label} run exited {result.returncode}.\n\n"
        f"stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    texts = _assistant_transcript_texts(db_path)
    joined = "\n".join(texts).strip()
    assert len(joined) >= _MIN_ASSISTANT_CHARS, (
        f"antigravity {label}: assistant transcript shorter than "
        f"{_MIN_ASSISTANT_CHARS} chars; got {joined!r}. The turn produced no "
        f"real reply.\n\nstdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    lowered = joined.lower()
    matched = [marker for marker in _ERROR_MARKERS if marker in lowered]
    assert not matched, (
        f"antigravity {label}: assistant transcript looks like an error item "
        f"(matched {matched}), not a real reply: {joined!r}\n\n"
        f"stderr:\n{result.stderr!r}"
    )
    return joined


def test_per_harness_antigravity_smoke(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    antigravity_spec: Path,
    tmp_path: Path,
) -> None:
    """A real antigravity turn returns a non-empty, non-error assistant reply.

    The end-to-end smoke gate: one ``omnigent run --harness antigravity -p``
    against the harness default model. Asserts over the persisted transcript
    (not stdout) that the assistant reply is >= ~10 chars and not an error
    string, and that the process exited 0.

    :param omnigent_python: Interpreter with omnigent + the antigravity SDK.
    :param omnigent_repo_root: Cwd for the subprocess.
    : param mock_credentials_env: Base env (PATH / onboarding-suppression /
        worktree PYTHONPATH); the Gemini key resolves independently of the
        Databricks gateway keys it also carries.
    :param antigravity_spec: Materialized antigravity agent YAML.
    :param tmp_path: Per-test temp dir (also the fake ``$HOME``).
    """
    skip_reason = _antigravity_skip_reason(omnigent_python)
    if skip_reason is not None:
        pytest.skip(skip_reason)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = _antigravity_env(mock_credentials_env, fake_home)

    result = _run_one_shot(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        spec_path=antigravity_spec,
        env=env,
        prompt="Reply in one short sentence that you are ready.",
        model=None,
    )
    _assert_clean_assistant_reply(fake_home / ".omnigent" / "chat.db", result, label="smoke")


def test_per_harness_antigravity_tool_parameter_schema_bridge(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    antigravity_tool_schema_spec: Path,
    tmp_path: Path,
) -> None:
    """A real antigravity turn can call a ToolSpec-backed structured-arg tool.

    This is the live e2e counterpart to
    ``test_sys_tool_schema_threaded_into_sdk_function_declaration``: it drives
    ``omnigent run --harness antigravity`` through the production YAML ->
    ToolSpec -> Antigravity SDK registration path, then asks the model to call a
    ``calculate(expression: str)`` tool. If the registered SDK callable loses
    the ToolSpec parameter schema, the model commonly calls the tool with the
    wrong argument shape or no argument at all; the expected tool lifecycle line
    and final result marker then do not appear.
    """
    skip_reason = _antigravity_skip_reason(omnigent_python)
    if skip_reason is not None:
        pytest.skip(skip_reason)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = _antigravity_env(dict(os.environ), fake_home)

    result = _run_one_shot(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        spec_path=antigravity_tool_schema_spec,
        env=env,
        prompt=(
            "Use the calculate tool to compute 48273 multiplied by 9182. "
            f"Do not solve it mentally. After the tool returns, reply exactly "
            f"'{_TOOL_SCHEMA_RESULT_MARKER}' and nothing else."
        ),
        model=_PINNED_MODEL,
    )
    joined = _assert_clean_assistant_reply(
        fake_home / ".omnigent" / "chat.db", result, label="tool-schema"
    )

    assert "calculate" in result.stdout.lower(), (
        "Antigravity did not render a calculate tool lifecycle line; the model "
        "likely did not call the structured-argument tool.\n\n"
        f"stdout:\n{result.stdout[-2500:]!r}\n\nstderr:\n{result.stderr[-2500:]!r}"
    )
    assert _TOOL_SCHEMA_RESULT_MARKER in joined.replace(",", "").lower(), (
        "Antigravity did not persist the expected calculate-tool result marker. "
        "If the ToolSpec parameter schema was not registered with the SDK, the "
        "model may have called calculate with a malformed argument object.\n\n"
        f"assistant transcript:\n{joined!r}\n\nstdout:\n{result.stdout[-2500:]!r}"
    )


@pytest.mark.parametrize(
    "model",
    [_PINNED_MODEL, _DEFAULT_MODEL],
    ids=["pinned-2.5-flash", "default-3.5-flash"],
)
def test_per_harness_antigravity_model_selection(
    model: str,
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    antigravity_spec: Path,
    tmp_path: Path,
) -> None:
    """A turn pinned to a valid Gemini id completes (and so does the default).

    Exercises ``--model`` threading through ``_build_antigravity_spawn_env`` ->
    ``HARNESS_ANTIGRAVITY_MODEL`` for an explicit ``gemini-2.5-flash`` pin, and
    the harness's resolved ``gemini-3.5-flash`` default. Both must exit 0 and
    persist a non-error assistant reply. Stays on the flash tier deliberately —
    ``gemini-3-pro`` 404s on a plain AI-Studio key.

    :param model: The Gemini id under test (parametrized).
    :param omnigent_python: Interpreter with omnigent + the antigravity SDK.
    :param omnigent_repo_root: Cwd for the subprocess.
    :param mock_credentials_env: Base subprocess env.
    :param antigravity_spec: Materialized antigravity agent YAML.
    :param tmp_path: Per-test temp dir (also the fake ``$HOME``).
    """
    skip_reason = _antigravity_skip_reason(omnigent_python)
    if skip_reason is not None:
        pytest.skip(skip_reason)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = _antigravity_env(mock_credentials_env, fake_home)

    result = _run_one_shot(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        spec_path=antigravity_spec,
        env=env,
        prompt="Name any primary color in one short sentence.",
        model=model,
    )
    _assert_clean_assistant_reply(
        fake_home / ".omnigent" / "chat.db", result, label=f"model={model}"
    )


def test_per_harness_antigravity_multi_turn_history_retention(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    antigravity_spec: Path,
    tmp_path: Path,
) -> None:
    """Turn 2 (``--continue``) references a nonce only turn 1 saw (#278).

    The antigravity SDK has no history-injection API, so #278 seeds prior turns
    onto a fresh/rebuilt session as a plain-text ``"Conversation so far: ..."``
    prefix. This is the integration test for that path: turn 1 (one-shot ``-p``)
    plants a unique nonce into the persistent store; turn 2 (interactive REPL
    ``--continue`` on the same store, with the recall prompt piped on stdin and
    ``/quit`` terminating it) must recover the nonce — proving the prior turn's
    text reached the model on the resumed session. The recall prompt does NOT
    contain the nonce, so the only way turn 2 can produce it is via the seeded
    history.

    The proof reads turn 2's **assistant transcript items** for the nonce, not
    stdout, keeping it consistent with the other assertions here.

    :param omnigent_python: Interpreter with omnigent + the antigravity SDK.
    :param omnigent_repo_root: Cwd for the subprocess.
    :param mock_credentials_env: Base subprocess env.
    :param antigravity_spec: Materialized antigravity agent YAML.
    :param tmp_path: Per-test temp dir (also the fake ``$HOME``).
    """
    skip_reason = _antigravity_skip_reason(omnigent_python)
    if skip_reason is not None:
        pytest.skip(skip_reason)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = _antigravity_env(mock_credentials_env, fake_home)
    db_path = fake_home / ".omnigent" / "chat.db"
    # Fresh per-run nonce so a parallel run can't leak it, and so the model can't
    # "recover" a popular fixture word from its training data instead of history.
    nonce = "nonce" + uuid.uuid4().hex[:12]

    # ── Turn 1: plant the nonce (one-shot -p, session-backed).
    plant = _run_one_shot(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        spec_path=antigravity_spec,
        env=env,
        prompt=(
            f"Remember the magic word {nonce}. Reply with one short sentence "
            f"that includes exactly that word."
        ),
        model=_PINNED_MODEL,
    )
    plant_text = _assert_clean_assistant_reply(db_path, plant, label="plant")
    # Pre-condition: the model actually echoed the nonce in turn 1. Without this,
    # turn 2's recovery would be meaningless (it could echo a nonce it never saw).
    assert nonce in plant_text.lower(), (
        f"turn 1 did not echo the nonce {nonce!r}; assistant text={plant_text!r}. "
        f"The multi-turn recovery check below would be meaningless."
    )
    assert db_path.is_file(), (
        f"persistent store not created at {db_path} — turn 1 ran ephemerally, so "
        f"--continue in turn 2 would have nothing to thread onto."
    )

    # ── Turn 2: recover the nonce via interactive REPL --continue. The CLI
    # rejects --continue + -p, so the recall prompt is piped on stdin and /quit
    # ends the REPL. The prompt deliberately omits the nonce.
    recall_prompt = (
        "What is the magic word I asked you to remember? Reply with one short "
        "sentence that includes exactly that word."
    )
    recall = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(antigravity_spec),
            "--harness",
            _HARNESS,
            "--model",
            _PINNED_MODEL,
            "--no-log",
            "--continue",
        ],
        env=env,
        cwd=str(omnigent_repo_root),
        input=f"{recall_prompt}\n/quit\n",
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert recall.returncode == 0, (
        f"turn 2 (--continue) exited {recall.returncode}.\n\n"
        f"stdout:\n{recall.stdout!r}\n\nstderr:\n{recall.stderr!r}"
    )
    # The load-bearing assertion: the nonce appears in an assistant transcript
    # item written after turn 1. Turn 2's reply is the newest assistant text, but
    # turn 1's reply also contained the nonce, so a stricter check (that the nonce
    # appears in MORE than one assistant message) confirms turn 2 reproduced it
    # rather than the assertion merely re-reading turn 1's persisted reply.
    texts = _assistant_transcript_texts(db_path)
    nonce_hits = sum(1 for text in texts if nonce in text.lower())
    assert nonce_hits >= 2, (
        f"nonce {nonce!r} appears in {nonce_hits} assistant message(s); expected "
        f">= 2 (turn 1's echo + turn 2's recovery). --continue failed to seed "
        f"turn 1's history onto the resumed antigravity session (#278).\n\n"
        f"assistant texts:\n{texts!r}\n\n"
        f"turn 2 stdout:\n{recall.stdout!r}\n\nturn 2 stderr:\n{recall.stderr!r}"
    )


def test_per_harness_antigravity_graceful_completion(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    antigravity_spec: Path,
    tmp_path: Path,
) -> None:
    """The happy path exits 0 and the parent session is not ``failed``.

    Distinct from the smoke test: this asserts the *graceful-completion*
    invariant — a clean exit and a transcript free of error markers, i.e. the
    session did not end in the ``failed`` state the executor uses for a glibc /
    model / egress failure (which would surface as an ``ExecutorError`` item, not
    a normal assistant reply). Together with the smoke test's content check this
    pins both "a reply came back" and "the turn completed cleanly".

    :param omnigent_python: Interpreter with omnigent + the antigravity SDK.
    :param omnigent_repo_root: Cwd for the subprocess.
    :param mock_credentials_env: Base subprocess env.
    :param antigravity_spec: Materialized antigravity agent YAML.
    :param tmp_path: Per-test temp dir (also the fake ``$HOME``).
    """
    skip_reason = _antigravity_skip_reason(omnigent_python)
    if skip_reason is not None:
        pytest.skip(skip_reason)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = _antigravity_env(mock_credentials_env, fake_home)
    db_path = fake_home / ".omnigent" / "chat.db"

    result = _run_one_shot(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        spec_path=antigravity_spec,
        env=env,
        prompt="Reply with one short friendly sentence.",
        model=None,
    )
    # _assert_clean_assistant_reply already checks exit 0 + no error markers in
    # the transcript; the persisted non-error assistant reply is the observable
    # proof the session reached a clean terminal state rather than ``failed``.
    _assert_clean_assistant_reply(db_path, result, label="graceful")
