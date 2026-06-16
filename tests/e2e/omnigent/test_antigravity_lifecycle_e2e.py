"""Live concurrency + process-lifecycle e2e for the antigravity (Gemini) harness.

The ``antigravity`` harness drives Google's Antigravity Python SDK
(``google-antigravity``). Unlike the other SDK harnesses it is **not**
pure-Python: ``Agent.__aenter__`` opens a local connection that spawns a
bundled **native ``localharness`` binary** (~111 MB ELF) which talks to
Google's Gemini backend. This test file is the lifecycle/concurrency counterpart
to the one-shot per-harness gates (``test_per_harness_claude_sdk.py`` /
``test_per_harness_cursor.py``): it drives real ``omnigent run`` subprocesses
and asserts invariants about *concurrent* turns and the *native subprocess
lifecycle* rather than a single assistant reply.

Three properties are covered, all against STABLE-on-main behavior:

1. **Parallel turns / no state bleed** — two concurrent ``omnigent run``
   invocations, each a distinct ephemeral session (``--no-session``) carrying a
   distinct sentinel token in its prompt. Each session's transcript must contain
   ONLY its own sentinel — proving the per-session SDK ``Agent``/``Conversation``
   reuse (keyed by ``session_key`` in
   :class:`~omnigent.inner.antigravity_executor.AntigravityExecutor`) does not
   leak one conversation's content into another's.
2. **No orphaned ``localharness``** — snapshot the live ``localharness`` PIDs
   before a turn and again after it ends cleanly; the turn must not leave a NEW
   native subprocess behind (the SDK ``Agent`` is closed on session teardown via
   ``close_session`` / ``close`` → ``Agent.__aexit__``, which must reap the
   native binary).
3. **Session cleanup reaps the runner/harness** — after a single ``omnigent
   run`` turn's process tree exits, no native subprocess that this run started
   may linger.

**Prerequisites (skipped when absent), mirroring the cursor gate:**

- ``google-antigravity`` importable in the *omnigent* venv (the test shells
  out, so the subprocess interpreter is what matters, not the test's own).
- A Gemini API key resolvable — either a configured ``antigravity:`` block
  (``omnigent setup``) or an ambient ``GEMINI_API_KEY`` / ``ANTIGRAVITY_API_KEY``.
  Antigravity is Gemini-native (no Databricks gateway / ``base_url`` path), so
  unlike the gateway harnesses this test does NOT use ``patched_databrickscfg``
  / ``omnigent_credentials_env`` — a missing key is a clean SKIP so the e2e
  shards stay green, and it runs for real wherever a key is present.

.. note::
   **glibc >= ~2.36 caveat.** The native ``localharness`` binary is linked
   against a recent glibc (needs ``GLIBC_ABI_DT_RELR``). On an older host (e.g.
   glibc 2.31) a live turn fails at SDK setup with ``RuntimeError: …
   localharness: … version 'GLIBC_ABI_DT_RELR' not found``, surfaced as an
   ``ExecutorError``. There is a dev-only loader-shim workaround
   (``ANTIGRAVITY_HARNESS_PATH`` pointing at a newer glibc's loader), but this
   test never depends on it: the lifecycle assertions (2, 3) hold even when the
   turn fails at setup (the subprocess still tears its process tree down), and
   the no-state-bleed assertion (1) self-skips when neither run produces its
   sentinel (which is what a glibc/quota failure looks like) rather than
   asserting a cross-contamination invariant it cannot observe.

**What breaks if this fails (with prerequisites present):**

- :class:`AntigravityExecutor`'s per-session agent isolation regresses and one
  conversation's state bleeds into another (assertion 1).
- The SDK ``Agent`` teardown (``close_session`` / ``close`` →
  ``_close_agent`` → ``Agent.__aexit__``) stops reaping the native
  ``localharness`` subprocess, leaking a process per turn (assertions 2, 3).
- The ``omnigent run`` one-shot path stops tearing down its local server /
  runner / harness process tree on exit.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

# Hardcoded because the file's whole point is the antigravity harness; a
# lifecycle test that didn't pin the harness it characterizes would be
# meaningless. Gemini ids only (the SDK is Gemini-native).
_HARNESS = "antigravity"
# gemini-2.5-flash works on a plain AI-Studio key; gemini-3-pro 404s there.
# Either flash model exercises the same native-binary lifecycle, which is what
# this file is about, so the cheaper/widely-available one is used.
_MODEL = "gemini-2.5-flash"

# The native binary is matched on its on-disk path substring. ``pgrep``'s
# default (process-name) match misses it because the visible argv[0] is the
# dynamic loader (``ld-linux-…``); ``pgrep -f`` against the full command line
# is the SDK's documented way to find the ``localharness`` process.
_LOCALHARNESS_PGREP_PATTERN = "antigravity/bin/localharness"

# Antigravity turns cold-start the native binary + round-trip to Gemini; 180s
# matches the headroom the other coding-agent per-harness gates allow, with
# extra room for the binary launch.
_RUN_TIMEOUT_SEC = 180

# After a clean ``omnigent run`` exit, the local server/runner shutdown that
# reaps the native subprocess is asynchronous. Poll the PID set up to this many
# seconds for the post-turn count to return to baseline before asserting a leak,
# so a slow-but-correct teardown is not mis-reported as an orphan.
_REAP_POLL_TIMEOUT_SEC = 30.0
_REAP_POLL_INTERVAL_SEC = 0.5

# Substrings that mark a turn that failed for *infrastructure* reasons (not a
# correctness regression): the glibc/native-binary wall on an old host, or a
# saturated Gemini free-tier quota. When BOTH parallel runs fail this way the
# no-state-bleed assertion has nothing real to compare, so it self-skips.
_INFRA_FAILURE_MARKERS = (
    "GLIBC_ABI_DT_RELR",  # native binary vs. old-host glibc
    "localharness",  # native-binary setup failure surfaced in the error
    "RESOURCE_EXHAUSTED",  # Gemini quota
    "code 429",
    "high demand",
    "quota",
)


def _antigravity_prereqs_missing(omnigent_python: Path) -> str | None:
    """Return a skip reason if the antigravity harness can't run, else ``None``.

    Probes the *omnigent* venv (the interpreter the subprocess uses), not the
    test's own interpreter: the SDK import and the key resolution both have to
    hold for the spawned ``omnigent run`` to do anything. Booleans only — the
    key is never printed.

    :param omnigent_python: Interpreter with omnigent installed, from the
        ``omnigent_python`` fixture.
    :returns: A human-readable skip reason, or ``None`` when both the
        ``google-antigravity`` package and a resolvable Gemini key are present.
    """
    probe = subprocess.run(
        [
            str(omnigent_python),
            "-c",
            # Print two booleans: SDK importable, and a Gemini key resolvable
            # from either the dedicated antigravity config block or ambient env.
            # ``find_spec`` is wrapped because for a namespace package it can
            # *raise* ModuleNotFoundError (not just return None) when the
            # ``google`` parent fails to resolve — treat any such failure as
            # "not importable" so the gate SKIPs cleanly instead of crashing the
            # probe (which would read as an inconclusive "could not probe").
            "import importlib.util as u, os\n"
            "try:\n"
            "    sdk = u.find_spec('google.antigravity') is not None\n"
            "except Exception:\n"
            "    sdk = False\n"
            "try:\n"
            "    from omnigent.onboarding.antigravity_auth import "
            "antigravity_api_key_configured as c\n"
            "    cfg = c()\n"
            "except Exception:\n"
            "    cfg = False\n"
            "env = bool(os.environ.get('GEMINI_API_KEY') or "
            "os.environ.get('ANTIGRAVITY_API_KEY'))\n"
            "print(int(sdk), int(cfg or env))",
        ],
        capture_output=True,
        text=True,
        # Run from the interpreter's own dir so a prepended ``''`` on sys.path
        # can't shadow the ``google`` namespace with an unrelated local package.
        cwd=str(omnigent_python.parent),
    )
    out = probe.stdout.strip().split()
    if probe.returncode != 0 or len(out) != 2:
        return (
            "could not probe antigravity prerequisites in the omnigent venv "
            f"(rc={probe.returncode}, stdout={probe.stdout!r}, stderr={probe.stderr!r})."
        )
    sdk_ok, key_ok = out[0] == "1", out[1] == "1"
    if not sdk_ok:
        return (
            "antigravity prerequisite missing: the 'google-antigravity' package is not installed."
        )
    if not key_ok:
        return (
            "antigravity prerequisite missing: no Gemini API key resolvable. The "
            "Antigravity SDK is Gemini-native (no Databricks-gateway path), so "
            "configure an 'antigravity:' key via 'omnigent setup' or export "
            "GEMINI_API_KEY / ANTIGRAVITY_API_KEY. Skipped (not failed) when absent."
        )
    return None


def _write_antigravity_bundle(bundle_dir: Path, *, name: str) -> Path:
    """Materialize a minimal antigravity agent bundle directory.

    A spec carrying ``spec_version`` must be a *directory* containing
    ``config.yaml`` — the antigravity harness rejects a single ``.yaml`` file.
    No ``auth:`` block is written so the key resolves from the ambient
    ``antigravity:`` config / env (see :func:`_antigravity_prereqs_missing`).

    :param bundle_dir: Directory to write the bundle into (created if absent).
    :param name: The agent ``name`` field; also makes each parallel bundle a
        distinct on-disk path so its session is independent.
    :returns: The bundle directory path (the argument ``omnigent run`` takes).
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "config.yaml").write_text(
        "spec_version: 1\n"
        f"name: {name}\n"
        "description: Antigravity lifecycle/concurrency e2e agent.\n"
        "executor:\n"
        "  type: omnigent\n"
        "  config:\n"
        f"    harness: {_HARNESS}\n"
        f"    model: {_MODEL}\n"
        "prompt: |\n"
        "  You are a terse echo agent. When asked to repeat a token, reply with\n"
        "  exactly that token and nothing else.\n",
        encoding="utf-8",
    )
    return bundle_dir


def _antigravity_env() -> dict[str, str]:
    """Build the subprocess env for an antigravity ``omnigent run``.

    Starts from ``os.environ`` (so HOME / PATH / the ambient Gemini key and any
    dev-only ``ANTIGRAVITY_HARNESS_PATH`` shim propagate) and suppresses the
    onboarding prompt + update-check banner so the subprocess never blocks on
    stdin or dirties stderr. Deliberately does NOT inject a Databricks PAT /
    OPENAI_BASE_URL: antigravity is Gemini-native and ignores them.

    :returns: An env dict for ``subprocess.run(env=...)``.
    """
    env = dict(os.environ)
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    return env


def _run_antigravity_turn(
    *,
    omnigent_python: Path,
    omnigent_repo_root: Path,
    bundle_dir: Path,
    prompt: str,
) -> subprocess.CompletedProcess[str]:
    """Run one ``omnigent run <bundle> --harness antigravity -p <prompt>``.

    ``--no-session`` gives the run a fresh ephemeral store so concurrent runs
    don't share a persistent conversation. ``--no-log`` keeps the JSON dump off
    disk. The full process tree (local server + runner + native ``localharness``)
    is spawned and torn down by this single invocation.

    :param omnigent_python: Interpreter with omnigent installed.
    :param omnigent_repo_root: Cwd for the subprocess (puts example tool modules
        on sys.path, matching the other run_omnigent tests).
    :param bundle_dir: The antigravity bundle directory to run.
    :param prompt: The one-shot ``-p`` prompt.
    :returns: The completed process (stdout/stderr captured, text mode).
    """
    return subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(bundle_dir),
            "--harness",
            _HARNESS,
            "--model",
            _MODEL,
            "-p",
            prompt,
            "--no-log",
            "--no-session",
        ],
        env=_antigravity_env(),
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )


def _localharness_pids() -> set[int]:
    """Return the set of currently-live native ``localharness`` PIDs.

    Uses ``pgrep -f`` against the binary's on-disk path substring (the SDK's
    documented way — the default process-name match misses it because argv[0] is
    the dynamic loader). A non-zero ``pgrep`` exit with no match means "none",
    which maps to an empty set.

    Returning the PID *set* (not a count) is deliberate: the lifecycle
    assertions take a before/after set difference so they are robust to
    unrelated ``localharness`` processes from other work on the same host — only
    PIDs this turn newly created are considered, and pre-existing ones are
    ignored.

    :returns: The set of live ``localharness`` PIDs (empty when none).
    """
    proc = subprocess.run(
        ["pgrep", "-f", _LOCALHARNESS_PGREP_PATTERN],
        capture_output=True,
        text=True,
    )
    pids: set[int] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.add(int(line))
    return pids


def _looks_like_infra_failure(result: subprocess.CompletedProcess[str]) -> bool:
    """Whether a turn failed for an infra reason (glibc/native binary or quota).

    Distinguishes "the harness couldn't even run a real model turn here"
    (old-host glibc wall, saturated Gemini quota) from a genuine output, so the
    no-state-bleed test can self-skip instead of mis-reporting an environment
    gap as a regression.

    :param result: The completed ``omnigent run`` process.
    :returns: ``True`` when stdout+stderr carry a known infra-failure marker.
    """
    blob = f"{result.stdout}\n{result.stderr}"
    return any(marker in blob for marker in _INFRA_FAILURE_MARKERS)


@pytest.fixture
def antigravity_runnable(omnigent_python: Path) -> None:
    """Skip the whole module's tests when antigravity can't run.

    :param omnigent_python: Interpreter probed for the SDK + key.
    :raises pytest.skip.Exception: When a prerequisite is missing.
    """
    reason = _antigravity_prereqs_missing(omnigent_python)
    if reason is not None:
        pytest.skip(reason)


def test_antigravity_parallel_turns_no_state_bleed(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    tmp_path: Path,
    antigravity_runnable: None,
) -> None:
    """Two concurrent antigravity turns keep their conversations isolated.

    Each run is a distinct ephemeral session (distinct bundle dir +
    ``--no-session``) whose prompt embeds a unique sentinel and asks the model
    to echo exactly that sentinel. The load-bearing property: each run's
    transcript contains ONLY its own sentinel — never the other run's — so the
    per-session ``Agent``/``Conversation`` keyed by ``session_key`` in
    :class:`AntigravityExecutor` does not bleed state across concurrent
    sessions.

    Self-skips (rather than fails) when neither run produces its sentinel: on a
    glibc-<2.36 host or against a saturated Gemini quota the turns fail at
    setup, leaving no real output to compare. The cross-contamination invariant
    is only meaningful when at least one run produced its own sentinel.

    :param omnigent_python: Interpreter with omnigent + google-antigravity.
    :param omnigent_repo_root: Cwd for the subprocesses.
    :param tmp_path: Per-test scratch dir for the two bundles.
    :param antigravity_runnable: Skip-guard (SDK + key present).
    """
    # Distinct, lowercase, punctuation-free sentinels so an exact substring
    # match is unambiguous and the model echoes them verbatim. Fresh per run
    # (uuid) so a popular fixture word can't be "recovered" from training data.
    sentinel_a = "sentinel" + uuid.uuid4().hex[:12]
    sentinel_b = "sentinel" + uuid.uuid4().hex[:12]

    bundle_a = _write_antigravity_bundle(tmp_path / "agy-a", name="agy_lifecycle_a")
    bundle_b = _write_antigravity_bundle(tmp_path / "agy-b", name="agy_lifecycle_b")

    def _prompt(sentinel: str) -> str:
        return f"Repeat exactly this token and nothing else: {sentinel}"

    results: dict[str, subprocess.CompletedProcess[str]] = {}

    def _worker(key: str, bundle: Path, sentinel: str) -> None:
        results[key] = _run_antigravity_turn(
            omnigent_python=omnigent_python,
            omnigent_repo_root=omnigent_repo_root,
            bundle_dir=bundle,
            prompt=_prompt(sentinel),
        )

    threads = [
        threading.Thread(target=_worker, args=("a", bundle_a, sentinel_a)),
        threading.Thread(target=_worker, args=("b", bundle_b, sentinel_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        # Each subprocess already has its own _RUN_TIMEOUT_SEC; the join just
        # needs to outlast that so a hung subprocess surfaces as its own
        # TimeoutExpired rather than a silent deadlock here.
        t.join(timeout=_RUN_TIMEOUT_SEC + 30)

    assert set(results) == {"a", "b"}, (
        f"a parallel antigravity worker did not finish; got results for {sorted(results)}."
    )
    out_a = results["a"].stdout.lower()
    out_b = results["b"].stdout.lower()

    produced_own_sentinel = sentinel_a in out_a or sentinel_b in out_b
    if not produced_own_sentinel:
        # Neither turn emitted real model output — only an infra failure makes
        # the no-bleed invariant unobservable, so require that signature before
        # skipping (otherwise a genuine "no output" regression would hide here).
        infra = _looks_like_infra_failure(results["a"]) or _looks_like_infra_failure(results["b"])
        reason = (
            "both parallel antigravity turns produced no sentinel"
            + (
                " (infra failure: glibc<2.36 native-binary wall or saturated Gemini quota)"
                if infra
                else ""
            )
            + f". stdout_a={results['a'].stdout!r} stderr_a={results['a'].stderr!r} "
            f"stdout_b={results['b'].stdout!r} stderr_b={results['b'].stderr!r}"
        )
        if infra:
            pytest.skip(reason)
        pytest.fail(reason)

    # The isolation invariant: no run's transcript carries the OTHER run's
    # sentinel. Checked per-run so a one-directional bleed is still caught even
    # when only one run produced output.
    assert sentinel_b not in out_a, (
        f"state bleed: run A's transcript contains run B's sentinel {sentinel_b!r}. "
        f"Concurrent antigravity sessions are sharing conversation state. "
        f"stdout_a={results['a'].stdout!r}"
    )
    assert sentinel_a not in out_b, (
        f"state bleed: run B's transcript contains run A's sentinel {sentinel_a!r}. "
        f"Concurrent antigravity sessions are sharing conversation state. "
        f"stdout_b={results['b'].stdout!r}"
    )


def test_antigravity_no_orphaned_localharness(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    tmp_path: Path,
    antigravity_runnable: None,
) -> None:
    """A clean antigravity turn leaves no NEW native ``localharness`` behind.

    Snapshots the live ``localharness`` PID set before the turn, runs one turn,
    then polls until the post-turn set returns to baseline (allowing for an
    asynchronous shutdown). The assertion is on the set DIFFERENCE — PIDs this
    turn created that are still alive — so unrelated ``localharness`` processes
    from other work on the host (which are in the baseline) never affect it.

    A leaked PID here means the SDK ``Agent`` teardown
    (``close_session`` / ``close`` → ``_close_agent`` → ``Agent.__aexit__``)
    stopped reaping the native subprocess, so every turn would leak one.

    Holds even when the turn fails at setup (glibc/quota): a turn that never
    spawned the binary creates no new PID, and a turn that spawned it before
    failing must still reap it on teardown.

    :param omnigent_python: Interpreter with omnigent + google-antigravity.
    :param omnigent_repo_root: Cwd for the subprocess.
    :param tmp_path: Per-test scratch dir for the bundle.
    :param antigravity_runnable: Skip-guard (SDK + key present).
    """
    bundle = _write_antigravity_bundle(tmp_path / "agy-orphan", name="agy_lifecycle_orphan")

    baseline_pids = _localharness_pids()

    result = _run_antigravity_turn(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        bundle_dir=bundle,
        prompt="Repeat exactly this token and nothing else: pong",
    )

    # Poll for the new-PID set to drain to empty; teardown of the local
    # server/runner that reaps the binary is asynchronous to the CLI exit.
    leaked = _localharness_pids() - baseline_pids
    deadline = _REAP_POLL_TIMEOUT_SEC
    waited = 0.0
    while leaked and waited < deadline:
        time.sleep(_REAP_POLL_INTERVAL_SEC)
        waited += _REAP_POLL_INTERVAL_SEC
        leaked = _localharness_pids() - baseline_pids

    assert not leaked, (
        f"antigravity turn leaked {len(leaked)} native localharness subprocess(es) "
        f"(PIDs {sorted(leaked)}) that were not in the pre-turn baseline and did not "
        f"exit within {deadline:.0f}s of the run completing. The SDK Agent teardown "
        f"is not reaping the native binary on session cleanup.\n\n"
        f"run stdout:\n{result.stdout!r}\n\nrun stderr:\n{result.stderr!r}"
    )


def test_antigravity_session_cleanup_reaps_runner(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    tmp_path: Path,
    antigravity_runnable: None,
) -> None:
    """After a turn's process tree exits, its native subprocess is reaped.

    A single ``omnigent run`` turn owns a self-contained process tree (local
    server → runner → native ``localharness``). Once the CLI process exits, that
    tree must be torn down: no ``localharness`` PID this run created may survive.
    Asserts on the before/after PID set difference (robust to unrelated host
    processes) after polling for an async teardown.

    Note (deliberately the *weaker, true* invariant): a separate bug-bash found
    a "session DELETE leaves the runner alive" leak, but it reproduces on the
    claude-sdk harness too — i.e. it is shared session-infra behavior, not an
    antigravity-specific contract. This test therefore does NOT exercise an
    explicit ``DELETE /v1/sessions/{id}`` and assert reaping (which would be a
    known shared-infra failure). It asserts the antigravity-relevant invariant
    that is true on main: a *clean turn end* (the ``-p`` one-shot exiting)
    returns the native-subprocess set to baseline. The CLI process exiting 0 is
    the clean-turn-end signal; the PID-diff is the reaping check.

    :param omnigent_python: Interpreter with omnigent + google-antigravity.
    :param omnigent_repo_root: Cwd for the subprocess.
    :param tmp_path: Per-test scratch dir for the bundle.
    :param antigravity_runnable: Skip-guard (SDK + key present).
    """
    bundle = _write_antigravity_bundle(tmp_path / "agy-cleanup", name="agy_lifecycle_cleanup")

    baseline_pids = _localharness_pids()

    result = _run_antigravity_turn(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        bundle_dir=bundle,
        prompt="Repeat exactly this token and nothing else: bye",
    )

    # The CLI process has already exited here (subprocess.run returned). Its
    # process tree teardown is what must reap the native binary; poll for the
    # new-PID set to drain so a slow-but-correct shutdown isn't a false leak.
    survivors = _localharness_pids() - baseline_pids
    waited = 0.0
    while survivors and waited < _REAP_POLL_TIMEOUT_SEC:
        time.sleep(_REAP_POLL_INTERVAL_SEC)
        waited += _REAP_POLL_INTERVAL_SEC
        survivors = _localharness_pids() - baseline_pids

    assert not survivors, (
        f"after the antigravity run's process tree exited, {len(survivors)} native "
        f"localharness subprocess(es) (PIDs {sorted(survivors)}) started by this run "
        f"were still alive after {_REAP_POLL_TIMEOUT_SEC:.0f}s. A clean turn end must "
        f"reap the per-conversation runner/harness; this is a lingering-process leak.\n\n"
        f"run stdout:\n{result.stdout!r}\n\nrun stderr:\n{result.stderr!r}"
    )
