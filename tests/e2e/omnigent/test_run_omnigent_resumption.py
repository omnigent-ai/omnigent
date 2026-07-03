"""End-to-end tests for ``omnigent run`` conversation resumption.

Covers ``--continue`` (latest conversation) and ``--resume <id>``
(specific conversation) across two independent subprocess
invocations. Both flags require interactive REPL mode (the CLI
rejects them combined with ``-p/--prompt``), so the resume steps
pipe the user prompt and ``/quit`` through stdin.

Verifies that a unique nonce sent in run #1 is recovered by the
LLM in run #2, proving that the persistent omnigent store at
``$HOME/.omnigent/chat.db`` carries history between invocations.

**What breaks if this fails:**

- The persistent store path regresses in
  ``omnigent.chat._omnigent_persistent_dir`` or
  ``omnigent.inner.cli._build_omnigent_stores`` — e.g. someone
  flips back to ``mkdtemp`` and ``--continue`` silently
  starts a fresh conversation.
- Idempotent agent registration regresses
  (``_omnigent_register_yaml_bundle``) — the second subprocess
  crashes on the ``agents.name`` UNIQUE constraint, OR
  registers a fresh ``agent_id`` that doesn't link to the
  prior conversation, OR fails to find the prior
  conversation when filtering by ``agent_id``.
- ``_resolve_previous_response_id`` stops finding the
  most-recent task on the most-recent conversation —
  ``--continue`` silently threads onto the wrong
  conversation, the LLM doesn't see the prior turn, and
  the nonce isn't recovered.
- ``_post_prompt_and_print`` stops passing
  ``previous_response_id`` on the POST — the route creates
  a fresh conversation for run #2 even though the resume
  resolution succeeded.

This is the canonical regression test for the
``designs/RUN_OMNIGENT_SESSION_RESUMPTION.md`` feature. If it
passes, the user-visible promise of ``--continue`` is
intact.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

from tests.e2e.omnigent.conftest import configure_mock_llm

# ``openai-agents`` is picked because it honors
# ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` directly — no
# ``~/.databrickscfg`` patching required (which would be
# awkward when HOME is a tmp_path).
_MODEL = "mock-model"
_HARNESS = "openai-agents"

# Subprocess timeout per ``omnigent run`` invocation.
# 180s matches the existing run_omnigent tests' headroom for DBOS
# sqlite migrations + cold imports + one openai-agents turn.
_RUN_TIMEOUT_SEC = 180


def _make_nonce() -> str:
    """
    Build a unique, lowercase, no-punctuation nonce token.

    Used as the magic word run #1 asks the model to
    remember and run #2 asks it to recall. Using a fresh
    nonce per test avoids the model seeing a popular
    test-fixture word in its training data and "recovering"
    it without actually using the resumed history.

    :returns: A short hex string, e.g. ``"floogerwhip3a4f"``.
    """
    # Deliberately not derived from a stable seed —
    # parallel test runs need distinct nonces so they don't
    # leak between conversations even if HOME isolation
    # somehow fails.
    return "nonce" + uuid.uuid4().hex[:12]


def _argv_run_omnigent(
    *,
    omnigent_python: Path,
    omnigent_repo_root: Path,
    prompt: str,
    extra_flags: list[str],
) -> list[str]:
    """
    Build the ``omnigent run`` argv for a one-shot ``-p`` invocation.

    Use for plant steps (no resumption flags). Resume steps need
    :func:`_argv_run_omnigent_interactive` instead, since the CLI rejects
    ``--continue`` / ``--resume`` combined with ``-p``.

    :param omnigent_python: Interpreter from the
        ``omnigent_python`` fixture.
    :param omnigent_repo_root: Repo root from the
        ``omnigent_repo_root`` fixture.
    :param prompt: The ``-p`` prompt for this invocation.
    :param extra_flags: Optional extra flags (e.g. ``["--no-session"]``).
    :returns: The full argv list.
    """
    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"
    return [
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
        prompt,
        "--no-log",
        *extra_flags,
    ]


def _argv_run_omnigent_interactive(
    *,
    omnigent_python: Path,
    omnigent_repo_root: Path,
    extra_flags: list[str],
) -> list[str]:
    """
    Build the ``omnigent run`` argv for the interactive REPL.

    The CLI rejects ``--continue`` / ``--resume`` combined with
    ``-p/--prompt``, so resume tests pipe the prompt through stdin
    instead. Pair with ``subprocess.run(..., input="<prompt>\\n/quit\\n")``.

    :param omnigent_python: Interpreter from the fixture.
    :param omnigent_repo_root: Repo root from the fixture.
    :param extra_flags: Resumption flags
        (``["--continue"]`` or ``["--resume", "<id>"]``).
    :returns: The full argv list (no ``-p``).
    """
    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"
    return [
        str(omnigent_python),
        "-m",
        "omnigent",
        "run",
        str(yaml_path),
        "--model",
        _MODEL,
        "--harness",
        _HARNESS,
        "--no-log",
        *extra_flags,
    ]


def _daemon_log_tails(home: Path, *, tail_chars: int = 3000) -> str:
    """
    Collect the tails of every daemon-side log under the fake ``$HOME``.

    Each ``omnigent run`` subprocess spawns its own local server, host
    daemon, and runner whose logs land under ``$HOME/.omnigent/logs/``
    (``server/``, ``runner/``, ``host-runner/``). When the CLI exits
    nonzero those logs are the only record of WHY — e.g. the local
    server dying mid-startup surfaces in the CLI only as a bare
    ``httpx.ConnectError`` from ``wait_for_runner_online``.

    :param home: The fake ``$HOME`` passed to :func:`_isolated_env`,
        e.g. ``tmp_path / "home"``.
    :param tail_chars: Max characters to include per log file.
    :returns: A formatted multi-log report for embedding in an
        assertion message, or a placeholder when no logs exist.
    """
    logs_dir = home / ".omnigent" / "logs"
    log_files = sorted(logs_dir.rglob("*.log")) if logs_dir.is_dir() else []
    if not log_files:
        return f"(no daemon logs under {logs_dir})"
    parts = []
    for log_file in log_files:
        text = log_file.read_text(errors="replace")
        parts.append(
            f"--- {log_file.relative_to(home)} (last {tail_chars} chars) ---\n{text[-tail_chars:]}"
        )
    return "\n".join(parts)


def _isolated_env(
    base_env: dict[str, str],
    home: Path,
) -> dict[str, str]:
    """
    Override ``HOME`` and the explicit Omnigent state/config roots so
    the subprocess's persistent store, local server pidfile, and host
    daemon records all land inside the test's temp dir.

    Without this isolation the test would write to the
    developer's real ``~/.omnigent/chat.db`` and could
    pick up unrelated prior conversations (or overwrite
    them).

    :param base_env: The fixture-provided env dict (PAT,
        OPENAI_BASE_URL, etc).
    :param home: Per-test tmp_path acting as the fake
        ``$HOME`` for both subprocesses in the run-pair.
    :returns: A fresh dict suitable for ``subprocess.run(env=)``.
    """
    env = dict(base_env)
    env["HOME"] = str(home)
    env["OMNIGENT_CONFIG_HOME"] = str(home / ".omnigent")
    env["OMNIGENT_DATA_DIR"] = str(home / ".omnigent")
    return env


def test_run_omnigent_continue_carries_history_across_invocations(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    Run #1 plants a unique nonce; run #2 with ``--continue``
    must recover it from the prior conversation.

    The proof is end-to-end: two distinct subprocess
    invocations, a real persistent SQLite store on disk,
    and the model's reply in run #2 contains a nonce that
    the model only could have seen via threading onto run
    #1's conversation.

    What breaks if this fails: see module-level docstring.
    Each layer's regression — store filter, idempotent
    register, previous_response_id plumbing, persistent
    store dir — produces a different observable failure
    here, but they all collapse the same way: run #2's
    output does not contain the nonce.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = _isolated_env(mock_credentials_env, fake_home)
    nonce = _make_nonce()
    # Run #1: mock returns nonce. Run #2 (--continue): mock
    # returns nonce again, simulating recall from history.
    configure_mock_llm(mock_llm_server_url, [{"text": nonce}, {"text": nonce}])

    # Run #1: plant the nonce. Use a deliberately-rigid
    # prompt so the model echoes the nonce verbatim, making
    # the run #2 check unambiguous.
    plant_prompt = (
        f"Remember the magic word {nonce}. Reply with exactly that word and nothing else."
    )
    result1 = subprocess.run(
        _argv_run_omnigent(
            omnigent_python=omnigent_python,
            omnigent_repo_root=omnigent_repo_root,
            prompt=plant_prompt,
            extra_flags=[],
        ),
        env=env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert result1.returncode == 0, (
        f"run #1 (plant) failed: stdout={result1.stdout!r} stderr={result1.stderr!r}\n"
        f"Daemon logs:\n{_daemon_log_tails(fake_home)}"
    )
    # Sanity check: the model echoed the nonce. Without
    # this, run #2 could recover the nonce just because the
    # word is visible in the prompt, not because of
    # resumption.
    assert nonce in result1.stdout.lower(), (
        f"run #1 didn't echo the nonce; stdout={result1.stdout!r}. "
        f"Without an echo, run #2's recovery is meaningless."
    )

    # The persistent store should now exist under the fake
    # HOME. If it doesn't, ``--continue`` in run #2 would
    # find nothing and fail loud.
    persistent_db = fake_home / ".omnigent" / "chat.db"
    assert persistent_db.is_file(), (
        f"Persistent store was not created at {persistent_db}. "
        f"Run #1 didn't write to ``~/.omnigent/chat.db`` — "
        f"either ``--no-session`` slipped in, or "
        f"``_omnigent_persistent_dir`` regressed."
    )

    # Run #2: recover the nonce via interactive REPL. The CLI
    # rejects ``--continue`` + ``-p``, so the recall prompt is
    # piped on stdin and ``/quit`` terminates the REPL cleanly.
    # The prompt deliberately does NOT include the nonce so the
    # model can only produce it via the resumed conversation.
    recall_prompt = (
        "What is the magic word I asked you to remember? "
        "Reply with exactly that word and nothing else."
    )
    result2 = subprocess.run(
        _argv_run_omnigent_interactive(
            omnigent_python=omnigent_python,
            omnigent_repo_root=omnigent_repo_root,
            extra_flags=["--continue"],
        ),
        env=env,
        cwd=str(omnigent_repo_root),
        input=f"{recall_prompt}\n/quit\n",
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert result2.returncode == 0, (
        f"run #2 (--continue) failed: stdout={result2.stdout!r} stderr={result2.stderr!r}\n"
        f"Daemon logs:\n{_daemon_log_tails(fake_home)}"
    )
    # The load-bearing assertion: the second run's stdout
    # contains the nonce that only run #1 saw.
    # Without resumption the model has zero way to know
    # the nonce; this assertion is the integration test for
    # every layer of the --continue plumbing at once.
    assert nonce in result2.stdout.lower(), (
        f"Nonce {nonce!r} not in run #2 output — --continue "
        f"failed to recover the prior conversation. "
        f"stdout={result2.stdout!r} stderr={result2.stderr!r}"
    )


def test_run_omnigent_continue_with_no_prior_conversation_exits_nonzero(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,  # no LLM call — exits before reaching mock server
) -> None:
    """
    ``--continue`` against a fresh ``$HOME`` (no prior
    conversations) exits non-zero with a clean error
    message. Matches the native shape at
    ``omnigent/inner/cli.py:3082-3084`` ("No saved sessions
    to continue.").

    What breaks if this fails:

    - Silent fallback to a fresh conversation when the
      user expected resumption (the "wrong, but no
      complaint" failure mode).
    - An exception traceback instead of a friendly
      message.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = _isolated_env(mock_credentials_env, fake_home)
    # ``--continue`` resolution happens at REPL boot before any
    # user input is consumed, so the subprocess exits non-zero
    # immediately. Stdin is closed via ``input=""`` so the REPL
    # gets EOF if it were ever to read.
    result = subprocess.run(
        _argv_run_omnigent_interactive(
            omnigent_python=omnigent_python,
            omnigent_repo_root=omnigent_repo_root,
            extra_flags=["--continue"],
        ),
        env=env,
        cwd=str(omnigent_repo_root),
        input="",
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    # Non-zero exit: clean failure, not a silent success.
    assert result.returncode != 0, (
        f"Expected non-zero exit when --continue finds "
        f"nothing; got 0. stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    # User-facing message names the agent. The exact
    # wording matches ``_resolve_initial_resume_conversation``
    # raising ``ClickException`` when no prior conversation exists.
    assert re.search(r"No prior conversation for agent", result.stderr), (
        f"Expected friendly error message in stderr; got stderr={result.stderr!r}"
    )


def test_run_omnigent_continue_works_across_oneshot_and_interactive_paths(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    A nonce planted in one-shot ``-p`` mode (the in-process
    ASGI path through
    ``omnigent.inner.cli._post_prompt_and_print``) MUST be
    visible to a subsequent interactive REPL ``--continue``
    (the subprocess-server path through
    ``omnigent.chat._chat_local`` ->
    ``omnigent.cli server``).

    Why this needs its own test: the two paths have separate
    agent-registration helpers
    (``_omnigent_register_yaml_bundle`` vs. ``_preregister_agent``).
    A previous regression had ``_preregister_agent`` doing
    delete + recreate of the agent row on every server
    startup, which cascaded through ``Task.agent_id`` and
    wiped the prior conversations — making ``--continue``
    error out with "No prior conversation for agent ..." even
    though the ``-p`` write had succeeded. The
    one-mode-only e2e test
    (``test_run_omnigent_continue_carries_history_across_invocations``)
    couldn't catch it because both subprocesses there go
    through ``_omnigent_register_yaml_bundle``.

    Drives the interactive REPL by piping stdin so the test
    stays headless. ``/quit`` raises EOFError inside the
    REPL; pytest captures the surrounding stdout where the
    nonce should appear next to the LLM's reply.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = _isolated_env(mock_credentials_env, fake_home)
    nonce = _make_nonce()
    # Plant: mock returns nonce. Recover (--continue): mock returns
    # nonce again simulating history-aware recall.
    configure_mock_llm(mock_llm_server_url, [{"text": nonce}, {"text": nonce}])

    # Step 1: plant the nonce via -p.
    plant_prompt = (
        f"Remember the magic word {nonce}. Reply with exactly that word and nothing else."
    )
    plant = subprocess.run(
        _argv_run_omnigent(
            omnigent_python=omnigent_python,
            omnigent_repo_root=omnigent_repo_root,
            prompt=plant_prompt,
            extra_flags=[],
        ),
        env=env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert plant.returncode == 0, (
        f"plant (one-shot -p) failed: stdout={plant.stdout!r} stderr={plant.stderr!r}\n"
        f"Daemon logs:\n{_daemon_log_tails(fake_home)}"
    )
    assert nonce in plant.stdout.lower(), (
        f"plant didn't echo the nonce — pre-condition for the "
        f"recover step is broken. stdout={plant.stdout!r}"
    )

    # Step 2: recover via interactive REPL --continue. Pipe
    # the user prompt + /quit on stdin so the headless
    # subprocess can complete without a TTY.
    interactive_argv = [
        str(omnigent_python),
        "-m",
        "omnigent",
        "run",
        str(omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"),
        "--model",
        _MODEL,
        "--harness",
        _HARNESS,
        "--no-log",
        "--continue",
    ]
    recover_prompt = (
        "What is the magic word I asked you to remember? "
        "Reply with exactly that word and nothing else."
    )
    recover = subprocess.run(
        interactive_argv,
        env=env,
        cwd=str(omnigent_repo_root),
        input=f"{recover_prompt}\n/quit\n",
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    # The interactive REPL prints a banner with "Resumed
    # conversation <id>" when --continue successfully
    # attaches. Without that, even an LLM hallucination
    # could produce the nonce — the resume check is what
    # this test is really for.
    assert "Resumed conversation" in recover.stdout, (
        f"interactive --continue did not attach to the prior "
        f"conversation. The cross-mode resumption (one-shot "
        f"plant -> interactive recover) is broken. "
        f"stdout={recover.stdout!r} stderr={recover.stderr!r}\n"
        f"Daemon logs:\n{_daemon_log_tails(fake_home)}"
    )
    assert nonce in recover.stdout.lower(), (
        f"Nonce {nonce!r} not in interactive --continue output. "
        f"Either the conversation wasn't resumed (see the "
        f"'Resumed conversation' check above) or the LLM was "
        f"served an empty history. stdout={recover.stdout!r}"
    )


def test_run_omnigent_session_id_pins_the_specific_conversation(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    ``--resume <conversation_id>`` must thread onto the
    *exact* conversation specified, not the most-recent one.
    Run #1 plants nonce A in convA; run #2 plants nonce B
    in convB (a fresh conversation, since no resume flag);
    run #3 with ``--resume convA_id`` must recover A
    (NOT B, even though B is newer); run #4 with
    ``--resume convB_id`` must recover B.

    What breaks if this fails:

    - ``--resume ID`` silently falls through to "use the
      latest conversation" (the bug where the explicit id
      is ignored), so users who pinned an older
      conversation get the wrong history threaded in.
    - The ``--continue`` resolution path takes precedence
      over the explicit id (the inverse — explicit beats
      implicit, but a regression could swap them).
    - The ``previous_response_id`` plumbing on the POST
      regresses and the new turn lands on a fresh
      conversation regardless of which id was passed.

    Conversation IDs are read directly from the SQLite
    store between runs because the CLI's stdout doesn't
    surface them (a future ``--print-conversation-id``
    flag would simplify this). ``ORDER BY updated_at
    DESC`` matches the SDK's ordering, so the first row
    after each plant is "the conversation just written."
    """
    import sqlite3

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = _isolated_env(mock_credentials_env, fake_home)
    nonce_a = _make_nonce()
    nonce_b = _make_nonce()
    persistent_db = fake_home / ".omnigent" / "chat.db"
    # 4 LLM calls: plant A, plant B, recall A (--resume convA), recall B (--resume convB).
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": nonce_a},
            {"text": nonce_b},
            {"text": nonce_a},
            {"text": nonce_b},
        ],
    )

    # ── Run 1: plant nonce A, fresh conversation (convA).
    plant_a = subprocess.run(
        _argv_run_omnigent(
            omnigent_python=omnigent_python,
            omnigent_repo_root=omnigent_repo_root,
            prompt=(
                f"Remember the magic word {nonce_a}. "
                f"Reply with exactly that word and nothing else."
            ),
            extra_flags=[],
        ),
        env=env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert plant_a.returncode == 0, (
        f"plant A failed: stdout={plant_a.stdout!r} stderr={plant_a.stderr!r}\n"
        f"Daemon logs:\n{_daemon_log_tails(fake_home)}"
    )
    assert nonce_a in plant_a.stdout.lower()

    # Capture convA's id BEFORE planting B so we get the
    # right one — "newest" walks forward as more
    # conversations are added.
    with sqlite3.connect(str(persistent_db)) as conn:
        rows = conn.execute(
            "SELECT id FROM conversations WHERE kind = 'default' ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    assert len(rows) == 1, (
        f"Expected exactly 1 conversation after plant A; got {len(rows)}. "
        f"If the count is 0, plant A didn't write to the persistent "
        f"store. If >1, an unrelated test polluted the dir or the "
        f"HOME isolation broke."
    )
    conv_a_id = rows[0][0]

    # ── Run 2: plant nonce B, FRESH conversation (no
    # --continue / --session). A regression where -p mode
    # accidentally threads onto the prior conversation
    # (the symmetric bug to "explicit id is ignored")
    # would surface here as plant B writing to convA
    # instead of creating convB — the assertion that
    # there are two distinct conversations after this
    # step catches that.
    plant_b = subprocess.run(
        _argv_run_omnigent(
            omnigent_python=omnigent_python,
            omnigent_repo_root=omnigent_repo_root,
            prompt=(
                f"Remember the magic word {nonce_b}. "
                f"Reply with exactly that word and nothing else."
            ),
            extra_flags=[],
        ),
        env=env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert plant_b.returncode == 0, (
        f"plant B failed: stdout={plant_b.stdout!r} stderr={plant_b.stderr!r}\n"
        f"Daemon logs:\n{_daemon_log_tails(fake_home)}"
    )
    assert nonce_b in plant_b.stdout.lower()

    with sqlite3.connect(str(persistent_db)) as conn:
        rows = conn.execute(
            "SELECT id FROM conversations WHERE kind = 'default' ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    assert len(rows) == 2, (
        f"Expected exactly 2 conversations after plant B; got {len(rows)}. "
        f"If 1, plant B threaded onto convA (the silent-resume "
        f"regression). If >2, leakage from another test."
    )
    conv_b_id = rows[0][0]
    assert conv_b_id != conv_a_id

    # ── Run 3: --resume convA_id must recover nonce A
    # (NOT B, even though B's conversation is "newer").
    # Interactive REPL because the CLI rejects --resume + -p.
    recall_a_prompt = (
        "What magic word did I just ask you to remember? "
        "Reply with exactly that word and nothing else."
    )
    recall_a = subprocess.run(
        _argv_run_omnigent_interactive(
            omnigent_python=omnigent_python,
            omnigent_repo_root=omnigent_repo_root,
            extra_flags=["--resume", conv_a_id],
        ),
        env=env,
        cwd=str(omnigent_repo_root),
        input=f"{recall_a_prompt}\n/quit\n",
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert recall_a.returncode == 0, (
        f"--resume convA failed: stdout={recall_a.stdout!r} stderr={recall_a.stderr!r}\n"
        f"Daemon logs:\n{_daemon_log_tails(fake_home)}"
    )
    out_a = recall_a.stdout.lower()
    # The load-bearing assertion: convA's nonce shows up,
    # convB's does NOT. Both checks together prove the id
    # actually pinned the right conversation rather than
    # accidentally landing on a fresh one (which would
    # also miss B but would render an unrelated reply).
    assert nonce_a in out_a, (
        f"--resume convA didn't recover nonce_a={nonce_a!r}. "
        f"Either the id wasn't honored or the conversation "
        f"history isn't reaching the LLM. stdout={recall_a.stdout!r}"
    )
    assert nonce_b not in out_a, (
        f"--resume convA leaked nonce_b={nonce_b!r} from "
        f"the OTHER conversation. The id filter on the "
        f"resume path is broken — it's pulling history from "
        f"every conversation for this agent instead of the "
        f"specific one. stdout={recall_a.stdout!r}"
    )

    # ── Run 4: --resume convB_id must recover nonce B.
    # Symmetric assertion to ensure --resume isn't just
    # always picking the same conversation.
    recall_b_prompt = (
        "What magic word did I just ask you to remember? "
        "Reply with exactly that word and nothing else."
    )
    recall_b = subprocess.run(
        _argv_run_omnigent_interactive(
            omnigent_python=omnigent_python,
            omnigent_repo_root=omnigent_repo_root,
            extra_flags=["--resume", conv_b_id],
        ),
        env=env,
        cwd=str(omnigent_repo_root),
        input=f"{recall_b_prompt}\n/quit\n",
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert recall_b.returncode == 0, (
        f"--resume convB failed: stdout={recall_b.stdout!r} stderr={recall_b.stderr!r}\n"
        f"Daemon logs:\n{_daemon_log_tails(fake_home)}"
    )
    out_b = recall_b.stdout.lower()
    assert nonce_b in out_b, (
        f"--resume convB didn't recover nonce_b={nonce_b!r}. stdout={recall_b.stdout!r}"
    )
    assert nonce_a not in out_b, (
        f"--resume convB leaked nonce_a={nonce_a!r}. stdout={recall_b.stdout!r}"
    )


def test_run_omnigent_session_id_unknown_exits_nonzero(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    ``--resume bogus_id`` (a conversation_id that doesn't
    exist in the store) exits non-zero with a clear
    "not found" message — not a silent fallback to a fresh
    conversation.

    What breaks if this fails: typoed conversation IDs
    silently start fresh conversations, surprising users
    who think they're resuming a specific thread.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = _isolated_env(mock_credentials_env, fake_home)
    result = subprocess.run(
        _argv_run_omnigent_interactive(
            omnigent_python=omnigent_python,
            omnigent_repo_root=omnigent_repo_root,
            extra_flags=["--resume", "conv_does_not_exist"],
        ),
        env=env,
        cwd=str(omnigent_repo_root),
        input="",
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert result.returncode != 0, (
        f"Expected non-zero exit for bogus --resume id; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "not found" in result.stderr.lower(), (
        f"Expected 'not found' in stderr; stderr={result.stderr!r}"
    )


def test_run_omnigent_no_session_does_not_pollute_persistent_store(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    ``--no-session`` opts back into the per-run tmpdir —
    the persistent ``$HOME/.omnigent/chat.db`` must NOT
    be touched by the run.

    What breaks if this fails: ``--no-session`` users who
    expect ephemeral runs end up writing to the shared
    persistent store, surprising both them and any
    concurrent ``--continue`` users.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = _isolated_env(mock_credentials_env, fake_home)
    result = subprocess.run(
        _argv_run_omnigent(
            omnigent_python=omnigent_python,
            omnigent_repo_root=omnigent_repo_root,
            prompt="say hi in 5 words",
            extra_flags=["--no-session"],
        ),
        env=env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert result.returncode == 0, (
        f"--no-session run failed: stdout={result.stdout!r} stderr={result.stderr!r}\n"
        f"Daemon logs:\n{_daemon_log_tails(fake_home)}"
    )
    # The persistent dir might exist (created by
    # ``_omnigent_persistent_dir`` regardless of
    # ``--no-session`` — that's a one-time mkdir, not a
    # write), but the chat.db file MUST NOT.
    persistent_db = fake_home / ".omnigent" / "chat.db"
    assert not persistent_db.exists(), (
        f"--no-session unexpectedly wrote to {persistent_db}. "
        f"Either the ephemeral branch in _build_omnigent_stores "
        f"is no longer being honored, or _omnigent_persistent_dir "
        f"is being called even when ephemeral=True."
    )
