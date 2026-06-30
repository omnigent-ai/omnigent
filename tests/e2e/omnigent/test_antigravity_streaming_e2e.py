"""Per-harness live characterization — antigravity (Gemini) SDK streaming fidelity.

Drives the ``antigravity`` harness end-to-end through the production
``/v1/sessions`` flow — register an inline Omnigent agent
(``executor.harness: antigravity``) → create + runner-bind a session →
``POST /v1/sessions/{id}/events`` → poll the session to idle — then asserts over
the *persisted transcript items* (``GET /v1/sessions/{id}/items``), NOT stdout
chrome. This exercises the full producer/consumer path in
:mod:`omnigent.inner.antigravity_executor`: ``conversation.send`` →
``receive_steps()`` → the MODEL→USER ``content_delta`` mapping onto
:class:`TextChunk` → the terminal :class:`TurnComplete` whose accumulated text
the runner persists as an assistant ``message`` item.

Scope is deliberately narrow: **streaming / output fidelity** of stable behavior
already on ``main`` (this branch is cut from ``main``). It covers three things a
streamed text channel can silently corrupt:

1. **Long output** is not truncated or duplicated — the per-delta accumulation
   in ``run_turn`` (``final_text_parts.append`` then ``"".join(...)``) must
   neither drop a tail nor replay a block.
2. **Unicode fidelity** — emoji / CJK / accented text round-trips byte-exact
   through ``content_delta`` concatenation and JSON (de)serialization, with no
   mojibake (``Ã©``), replacement char (``�``), or double-encoding.
3. **Reasoning-heavy task** — a step-by-step prompt still yields a coherent,
   non-empty final answer (we assert the answer sentinel, not the reasoning
   internals, which the harness maps separately as :class:`ReasoningChunk`).

**Gating (mirrors the cursor per-harness test):** the antigravity harness is
Gemini-native — it authenticates with a Gemini / Antigravity API key and has NO
Databricks-gateway path — so this test SKIPS (rather than fails) when its
prerequisites are absent, keeping the e2e shards green where no Gemini key is
provisioned. It runs for real wherever the prerequisites are present:

- ``google.antigravity`` (the ``google-antigravity`` package) is importable, and
- a Gemini key is configured (``antigravity_api_key_configured()``) or present
  ambiently as ``GEMINI_API_KEY`` / ``ANTIGRAVITY_API_KEY``.

.. note::
   **glibc caveat.** The Antigravity SDK launches a bundled native
   ``localharness`` binary linked against a recent glibc (it needs
   ``GLIBC_ABI_DT_RELR``), so a turn only *completes* on a host with
   glibc ≳ 2.36. On an older host (e.g. glibc 2.31) the turn fails at native
   setup with ``version 'GLIBC_ABI_DT_RELR' not found`` and the session goes
   ``failed`` — that is an environment gap, not a regression in the code under
   test. The gate above mirrors cursor's (package + key) and deliberately does
   NOT probe glibc; on a glibc-2.31 dev box point the SDK at a loader-shim via
   ``ANTIGRAVITY_HARNESS_PATH`` (dev-only) to run the untouched binary under a
   newer loader. CI runs this on a glibc-≥2.36 host.

**What breaks if this fails (with prerequisites present):**
- ``AntigravityExecutor`` regresses (the ``Step`` → :class:`TextChunk` mapping,
  the per-turn text accumulation in ``run_turn``, or the MODEL→USER source/target
  filter that keeps the echoed prompt out of the assistant reply).
- ``_build_antigravity_spawn_env`` stops resolving the Gemini key into
  ``HARNESS_ANTIGRAVITY_API_KEY`` for a spec with no ``executor.auth``.
- The sessions dispatch path stops persisting the streamed assistant text into
  ``conversation_items`` faithfully (truncation, duplication, or re-encoding).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.onboarding.antigravity_auth import antigravity_api_key_configured
from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
    send_user_message_to_session,
    upload_agent,
)

# Gemini id that runs on a plain AI-Studio key. ``gemini-3-pro`` 404s without
# Pro access, so the suite pins a Flash model; left un-rewritten (the harness is
# Gemini-native — a Databricks model map / profile stamp would break it).
_MODEL = "gemini-3.5-flash"
_HARNESS = "antigravity"

# A turn cold-starts the native binary, round-trips to Google's backend, and
# (for the long-output case) streams a multi-paragraph reply. 240s matches the
# headroom the other live session smokes allow without letting a hung run pin
# the suite forever.
_RUN_TIMEOUT_SEC = 240.0


def _antigravity_skip_reason() -> str | None:
    """Return why the antigravity harness can't run for real, or ``None``.

    Mirrors the cursor per-harness gate's spirit: the harness is Gemini-native
    (no Databricks-gateway fallback), so when its prerequisites are absent the
    suite SKIPS rather than fails — CI shards without a provisioned Gemini key
    stay green, and the test runs for real wherever a key is present.

    Two prerequisites, both probed against the *running* interpreter (the e2e
    server + runner the ``live_server`` fixture spawns with ``sys.executable``
    are this same interpreter):

    1. ``google.antigravity`` is importable (the ``google-antigravity`` extra).
    2. A Gemini key is configured (the dedicated ``antigravity:`` config block)
       or present ambiently as ``GEMINI_API_KEY`` / ``ANTIGRAVITY_API_KEY`` —
       which ``_build_antigravity_spawn_env`` threads into the harness as
       ``HARNESS_ANTIGRAVITY_API_KEY`` for a spec with no ``executor.auth``.

    This gate intentionally does NOT probe glibc (see the module docstring's
    note): on a glibc-<2.36 host the prerequisites read as present and the turn
    fails at native-binary setup — surfaced by the per-turn terminal-status
    assertion as an environment gap, not masked as a silent skip.

    :returns: A skip-reason string when a prerequisite is missing, else ``None``.
    """
    if importlib.util.find_spec("google.antigravity") is None:
        return (
            "antigravity prerequisite missing: the 'google-antigravity' package "
            "is not importable. Install it with: pip install 'omnigent[antigravity]'."
        )
    key_present = antigravity_api_key_configured() or bool(
        os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTIGRAVITY_API_KEY")
    )
    if not key_present:
        return (
            "antigravity prerequisite missing: no Gemini key configured. The "
            "Antigravity SDK requires one (there is no login flow); configure it "
            "via 'omni setup' (Antigravity) or export GEMINI_API_KEY / "
            "ANTIGRAVITY_API_KEY. Skipped (not failed) so CI shards without a key "
            "stay green."
        )
    return None


# Skip the whole module at COLLECTION time when the harness can't run. This must
# precede fixture setup: the live-server stack here is session-scoped (and the
# e2e suite's own ``--llm-api-key`` gate lives on a session fixture), and
# higher-scoped fixtures resolve before any function-scoped skip-guard could
# fire — so a function-level gate would surface as a fixture ERROR instead of a
# clean SKIP when a prerequisite is absent. ``allow_module_level`` runs the gate
# before any of that, yielding a clean skip with no server/runner spawned.
_SKIP_REASON = _antigravity_skip_reason()
if _SKIP_REASON is not None:
    pytest.skip(_SKIP_REASON, allow_module_level=True)


def _write_antigravity_agent_yaml(tmp_path: Path, *, prompt: str) -> Path:
    """Write a minimal single-file ``harness: antigravity`` Omnigent bundle.

    Deliberately carries NO ``executor.auth`` and NO Databricks ``profile``: the
    harness resolves its Gemini key Gemini-natively (configured ``antigravity:``
    block, then ambient ``GEMINI_API_KEY`` / ``ANTIGRAVITY_API_KEY``) — a global
    ``auth:`` / profile would be the OpenAI-gateway key the SDK can't use. The
    bundle is uploaded with ``rewrite_model_for_databricks=False`` so the Gemini
    model id and ``antigravity`` harness pass through unmangled.

    :param tmp_path: Per-test temp dir to materialize the bundle directory in.
    :param prompt: The agent's system prompt (per-test, so each scenario can
        steer the model toward a deterministic, assertable shape).
    :returns: The bundle directory to hand to :func:`upload_agent`.
    """
    agent_dir = tmp_path / "antigravity-streaming-agent"
    agent_dir.mkdir()
    (agent_dir / "antigravity-streaming-agent.yaml").write_text(
        "\n".join(
            [
                "name: antigravity-streaming-agent",
                "description: Live streaming/output-fidelity probe for the antigravity harness.",
                "executor:",
                f"  harness: {_HARNESS}",
                f"  model: {_MODEL}",
                "prompt: |",
                *(f"  {line}" for line in prompt.splitlines()),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return agent_dir


def _run_one_shot(
    http_client: httpx.Client,
    *,
    runner_id: str,
    agent_name: str,
    user_text: str,
) -> str:
    """Run one antigravity turn through a fresh runner-bound session.

    Creates a session, posts *user_text*, and polls to terminal — asserting the
    turn completed (a ``failed`` status surfaces the error, e.g. the glibc
    native-binary failure on an old host, instead of masquerading as empty
    output).

    :param http_client: HTTP client pointed at the live server.
    :param runner_id: The live runner id to bind the session to.
    :param agent_name: The registered antigravity agent's name.
    :param user_text: The user prompt for this turn.
    :returns: The session id (so the caller can read ``/items``).
    """
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=runner_id
    )
    response_id = send_user_message_to_session(
        http_client, session_id=session_id, content=user_text
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert body["status"] == "completed", (
        f"antigravity turn did not complete: status={body['status']!r}, "
        f"error={body.get('error')!r}. A 'failed' status with a "
        f"GLIBC_ABI_DT_RELR error means the host's glibc is < 2.36 (the SDK's "
        f"native binary can't load) — an environment gap, not a code "
        f"regression. output={body.get('output')!r}"
    )
    return session_id


def _assistant_text_from_items(http_client: httpx.Client, session_id: str) -> str:
    """Concatenate assistant ``output_text`` from ``GET /v1/sessions/{id}/items``.

    Reads the persisted transcript (the durable record the production session
    flow commits), NOT stdout, so the assertions characterize what the harness
    actually streamed and stored. The items endpoint returns the flat
    Responses-style item shape — assistant text lives in ``message`` items with
    ``role == "assistant"`` whose ``content`` carries ``output_text`` blocks
    (see ``omnigent/server/API.md`` § List Conversation Items).

    :param http_client: HTTP client pointed at the live server.
    :param session_id: The session/conversation id to read.
    :returns: The assistant text, ``"\\n"``-joined across any assistant messages
        in the turn (empty string if none).
    """
    resp = http_client.get(f"/v1/sessions/{session_id}/items", params={"limit": 1000})
    resp.raise_for_status()
    items: list[dict[str, Any]] = resp.json()["data"]
    parts: list[str] = []
    for item in items:
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        for block in item.get("content", []):
            if block.get("type") == "output_text":
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


def _max_repeated_run(text: str, *, block: int) -> int:
    """Return the largest count of any *block*-char substring repeated in *text*.

    A cheap duplication detector: a streamed reply that double-appended a chunk
    shows up as a long substring occurring far more than the prose would warrant.
    Samples non-overlapping windows so it stays linear on long replies.

    :param text: The text to scan.
    :param block: Window size in characters; large enough that natural prose
        rarely repeats a window verbatim.
    :returns: The max occurrence count of any sampled window (1 when nothing
        repeats; >= 2 indicates a verbatim repeat of a *block*-sized span).
    """
    if len(text) < block:
        return 1
    counts: dict[str, int] = {}
    best = 1
    for start in range(0, len(text) - block + 1, block):
        window = text[start : start + block]
        counts[window] = counts.get(window, 0) + 1
        best = max(best, counts[window])
    return best


# ─── Tests ───────────────────────────────────────────────────


def test_antigravity_long_output_not_truncated_or_duplicated(
    http_client: httpx.Client,
    live_runner_id: str,
    tmp_path: Path,
) -> None:
    """A long multi-paragraph reply streams in full — not truncated, not duplicated.

    The model is steered to emit six numbered sections, each ending in a unique
    ``[SECT-k-END]`` sentinel. Asserting every sentinel survives proves nothing
    was dropped from the head or tail of the streamed text; a length floor
    proves the reply is genuinely substantial; and a verbatim-repeat check
    proves the per-delta accumulation didn't replay a block. Together these
    characterize ``run_turn``'s ``final_text_parts`` accumulation over a long
    ``content_delta`` stream.
    """
    sentinels = [f"[SECT-{k}-END]" for k in range(1, 7)]
    section_lines = "\n".join(
        f"- Section {k}: at least four full sentences, then end the section with "
        f"the exact marker {sent}"
        for k, sent in enumerate(sentinels, start=1)
    )
    agent_name = upload_agent(
        http_client,
        _write_antigravity_agent_yaml(
            tmp_path,
            prompt=(
                "You are a thorough technical writer. When asked for a multi-section "
                "explanation, write every requested section in full, each as its own "
                "paragraph, and place each section's exact end-marker on its own line. "
                "Never abbreviate, summarize, or skip a section."
            ),
        ),
        rewrite_model_for_databricks=False,
    )
    session_id = _run_one_shot(
        http_client,
        runner_id=live_runner_id,
        agent_name=agent_name,
        user_text=(
            "Write a detailed explanation of how a CPU cache hierarchy works, "
            "organized into exactly these six sections (keep them in order):\n"
            f"{section_lines}\n"
            "Write each section fully before moving to the next."
        ),
    )

    text = _assistant_text_from_items(http_client, session_id)

    # Not truncated: every section's end-marker survived the stream. A dropped
    # tail (the classic truncation bug) loses the later markers first.
    missing = [s for s in sentinels if s not in text]
    assert not missing, (
        f"long-output reply is missing section markers {missing} — the streamed "
        f"text was truncated before the end. Got {len(text)} chars:\n{text!r}"
    )

    # Substantial: six full sections clear this floor comfortably; an empty /
    # one-line reply (a streaming or accumulation regression) does not.
    assert len(text) >= 800, (
        f"long-output reply is only {len(text)} chars; expected a substantial "
        f"multi-section answer (>= 800). Reply:\n{text!r}"
    )

    # Not duplicated: no 80-char window recurs verbatim. A double-appended delta
    # block would push some window's count to >= 2.
    repeats = _max_repeated_run(text, block=80)
    assert repeats < 2, (
        f"long-output reply repeats an 80-char block {repeats}x — the stream "
        f"likely double-appended a chunk. Reply:\n{text!r}"
    )


def test_antigravity_unicode_fidelity_round_trips(
    http_client: httpx.Client,
    live_runner_id: str,
    tmp_path: Path,
) -> None:
    """Emoji + CJK + accented text round-trips byte-exact into the transcript.

    The model is asked to echo a fixed payload verbatim on its own line. We
    assert each non-ASCII fragment appears EXACTLY in the persisted item text —
    catching mojibake (``café`` → ``cafÃ©``), the replacement char ``\\ufffd``,
    and double-encoding, any of which would corrupt the ``content_delta``
    concatenation or the JSON (de)serialization on the way into
    ``conversation_items``.
    """
    # One emoji (incl. a ZWJ sequence + skin-tone modifier), CJK, and accents.
    fragments = ["café", "naïve façade", "日本語のテキスト", "中文字符", "🚀", "👩‍🚀", "👍🏽"]
    payload = " | ".join(fragments)
    agent_name = upload_agent(
        http_client,
        _write_antigravity_agent_yaml(
            tmp_path,
            prompt=(
                "You echo text exactly as given, preserving every character including "
                "emoji, CJK, and accents. Never transliterate, escape, or normalize."
            ),
        ),
        rewrite_model_for_databricks=False,
    )
    session_id = _run_one_shot(
        http_client,
        runner_id=live_runner_id,
        agent_name=agent_name,
        user_text=(
            "Repeat the following text back to me EXACTLY, character for character, "
            "on a single line, with no additional commentary, quotes, or code "
            f"fences:\n{payload}"
        ),
    )

    text = _assistant_text_from_items(http_client, session_id)

    # No corruption signatures anywhere in the persisted text.
    assert "�" not in text, (
        f"transcript contains the Unicode replacement char (\\ufffd) — bytes were "
        f"lost or mis-decoded in the stream. Reply:\n{text!r}"
    )
    # ``Ã`` is the tell-tale lead byte of UTF-8 mis-decoded as Latin-1 (e.g.
    # ``é`` → ``Ã©``); none of our intended fragments contain it.
    assert "Ã" not in text and "â€" not in text, (
        f"transcript shows mojibake / double-encoding (e.g. 'Ã©' for 'é') — a "
        f"decode/encode mismatch corrupted the non-ASCII text. Reply:\n{text!r}"
    )
    # Each intended fragment must survive verbatim.
    missing = [frag for frag in fragments if frag not in text]
    assert not missing, (
        f"these unicode fragments did not round-trip into the transcript: "
        f"{missing}. The harness must preserve emoji/CJK/accents byte-exact. "
        f"Reply:\n{text!r}"
    )


def test_antigravity_reasoning_heavy_task_final_answer(
    http_client: httpx.Client,
    live_runner_id: str,
    tmp_path: Path,
) -> None:
    """A step-by-step task yields a coherent, non-empty final answer.

    A small arithmetic word problem elicits multi-step reasoning; the model is
    told to end with ``FINAL ANSWER: <n>``. We assert the answer sentinel with
    the correct value is present and the reply is non-trivial — characterizing
    that the harness streams a usable final answer for a reasoning-heavy turn.
    We deliberately do NOT assert on the reasoning internals (the harness maps
    ``thinking_delta`` onto :class:`ReasoningChunk` separately, and whether/how
    much reasoning surfaces is model- and config-dependent — over-specifying it
    would make the test flaky without testing output fidelity).
    """
    agent_name = upload_agent(
        http_client,
        _write_antigravity_agent_yaml(
            tmp_path,
            prompt=(
                "You are a careful problem solver. Work through the problem step by "
                "step, then state the result on a final line in the exact form "
                "'FINAL ANSWER: <number>'."
            ),
        ),
        rewrite_model_for_databricks=False,
    )
    # 3 crates * 4 boxes * 6 widgets = 72; +5 spares = 77. A unique integer the
    # model is very unlikely to emit except as the correct result.
    session_id = _run_one_shot(
        http_client,
        runner_id=live_runner_id,
        agent_name=agent_name,
        user_text=(
            "A warehouse has 3 crates. Each crate holds 4 boxes, and each box holds "
            "6 widgets. There are also 5 loose spare widgets on a shelf. How many "
            "widgets are in the warehouse in total? Show your reasoning step by "
            "step, then end with a line 'FINAL ANSWER: <number>'."
        ),
    )

    text = _assistant_text_from_items(http_client, session_id)

    # Coherent + non-empty: a reasoning-heavy reply is more than a bare token.
    assert len(text.strip()) >= 20, (
        f"reasoning-task reply is too short to be a coherent answer "
        f"({len(text.strip())} chars): {text!r}"
    )
    # The final answer surfaced with the correct value. ``77`` (not ``72``) also
    # confirms the model accounted for the spares — i.e. the streamed reply
    # carries the genuine end-of-reasoning result, not a truncated mid-step.
    assert "FINAL ANSWER" in text, (
        f"reply is missing the requested 'FINAL ANSWER:' line — the final-answer "
        f"text didn't stream into the transcript. Reply:\n{text!r}"
    )
    assert "77" in text, (
        f"reply's final answer is not 77 (3*4*6 + 5); the reasoning result is "
        f"wrong or its tail was dropped from the stream. Reply:\n{text!r}"
    )
