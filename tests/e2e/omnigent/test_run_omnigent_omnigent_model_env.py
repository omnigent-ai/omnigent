"""E2E coverage for the ``OMNIGENT_MODEL`` env-var fallback on ``omnigent run``.

The fallback fires in ``omnigent/chat.py:_apply_overrides_to_raw`` when the
spec has no ``executor.model`` / ``executor.harness`` and no ``--model`` /
``--harness`` flag is passed. Helper-level coverage lives in
``tests/cli/test_chat.py``; this file spawns a real subprocess so a regression
between the helper and the FM API surfaces too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.e2e._run_with_group_timeout import run_with_group_timeout

_VALID_MODEL = "databricks-gpt-5-4-mini"

# ``databricks-gpt-`` prefix is load-bearing on two counts:
# 1. ``databricks-`` exempts ``llm.connection`` from
#    ``omnigent.spec.validator._validate_executor_llm``; any other prefix
#    rejects the YAML before any FM API call happens.
# 2. ``databricks-gpt-`` routes through ``omnigent.llms.routing.infer_
#    harness_from_model`` to ``openai-agents``; a bare ``databricks-`` prefix
#    leaves ``executor.harness=""`` and the runtime wedges (no validator
#    catches the empty harness when ``llm.model`` is set).
_BOGUS_MODEL = "databricks-gpt-this-model-does-not-exist-omnigent-env-test-9f3a"

_PROMPT = "say hi in 5 words"
# Wall-clock budget for the subprocess. ``omnigent run`` spawns the
# AP server + runner as grandchildren, so a plain ``subprocess.run``
# timeout could not reap them — the grandchildren kept the captured
# pipe open and ``communicate()`` wedged the shard ~15+ min past the
# nominal timeout (the bug that suppressed
# ``test_omnigent_model_env_var_bogus_value_fails_with_named_error``).
# ``run_with_group_timeout`` SIGKILLs the whole process group at the
# deadline, so the budget below is a hard ceiling regardless of how
# the grandchildren behave. A bogus model 404s on the first FM API
# call (404 is not in the SDK's retryable set), so the negative case
# resolves in seconds; the positive sibling is one short turn. 120s
# covers server+runner cold-start plus a slow gateway day on the
# positive path while staying under the CI per-test --timeout=180
# cap, so the group cleanup fires before pytest's thread-timeout
# gives up. Either way the shard can no longer wedge for minutes.
_RUN_TIMEOUT_SEC = 120.0
_MIN_ASSISTANT_CHARS = 4

_MINIMAL_YAML = (
    "name: hello_world\nprompt: You are a friendly assistant. Say hello and answer questions.\n"
)


def _run_omnigent_with_model_env(
    *,
    model_env_value: str,
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
    tmp_path: Path,
) -> subprocess.CompletedProcess[str]:
    """
    Run ``omnigent run <minimal>.yaml -p "..."`` with ``OMNIGENT_MODEL`` set.

    Writes a minimal no-``executor`` YAML to *tmp_path*; reusing the shared
    ``hello_world.yaml`` would defeat the test because that file declares
    ``executor.model``, which short-circuits the env-var fallback gate.

    Uses :func:`run_with_group_timeout` rather than ``subprocess.run``
    because ``omnigent run`` spawns the AP server + runner as
    grandchildren in the same process group; a stock ``subprocess.run``
    timeout only kills the immediate child, leaving the grandchildren to
    hold the captured pipe open and wedge ``communicate()`` long past the
    deadline.

    :param model_env_value: ``OMNIGENT_MODEL`` value (real or bogus).
    :param tmp_path: Per-test tmp dir for the minimal YAML.
    :returns: Subprocess result with stdout/stderr captured as text.
    :raises subprocess.TimeoutExpired: When the run exceeds
        ``_RUN_TIMEOUT_SEC``; the whole process group is SIGKILLed and
        any captured stdout/stderr is attached to the exception.
    """
    yaml_path = tmp_path / "hello_world_no_executor.yaml"
    yaml_path.write_text(_MINIMAL_YAML)
    env = dict(omnigent_credentials_env)
    env["OMNIGENT_MODEL"] = model_env_value
    return run_with_group_timeout(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "-p",
            _PROMPT,
            "--no-session",
        ],
        env=env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )


def test_omnigent_model_env_var_drives_successful_run(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Smoke test: a real model in ``OMNIGENT_MODEL`` produces a successful turn.

    A pass alone doesn't prove the env var was honored (the default model also
    succeeds); the bogus-value sibling carries the decisive proof. This test
    catches the env-var path going from "silently dropped" to "actively broken".
    """
    result = _run_omnigent_with_model_env(
        model_env_value=_VALID_MODEL,
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        omnigent_credentials_env=omnigent_credentials_env,
        tmp_path=tmp_path,
    )

    # Non-zero exit means either the env var never reached the executor block
    # or the resolved model failed at the FM API — both silently break users.
    assert result.returncode == 0, (
        f"omnigent run with OMNIGENT_MODEL={_VALID_MODEL!r} exited "
        f"with code {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    # Short / empty reply hints at a downgraded model or output-extraction regression.
    text = result.stdout.strip()
    assert len(text) >= _MIN_ASSISTANT_CHARS, (
        f"Expected assistant reply >= {_MIN_ASSISTANT_CHARS} chars; "
        f"got {len(text)} (stdout={text!r})."
    )


def test_omnigent_model_env_var_bogus_value_fails_with_named_error(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Decisive test: a bogus ``OMNIGENT_MODEL`` fails with the bogus name in stderr.

    A failure that names the sentinel can only happen if the env-var value
    traveled the full pipeline to the FM API. If the env var were silently
    dropped, the default model would succeed (or fail with its own name).
    """
    result = _run_omnigent_with_model_env(
        model_env_value=_BOGUS_MODEL,
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        omnigent_credentials_env=omnigent_credentials_env,
        tmp_path=tmp_path,
    )

    # Exit 0 means the env var was dropped and the default model took over.
    assert result.returncode != 0, (
        f"omnigent run with OMNIGENT_MODEL={_BOGUS_MODEL!r} unexpectedly "
        f"succeeded (exit 0); the env var was silently dropped.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    # Assumes the Databricks FM API echoes the requested model name in 404 /
    # 400 responses. If the API stops echoing, ``tests/cli/test_chat.py``
    # is the helper-level backstop.
    combined = result.stdout + result.stderr
    assert _BOGUS_MODEL in combined, (
        f"Bogus model {_BOGUS_MODEL!r} not in subprocess output — either the "
        f"env var was dropped and the default model took over, or the FM API "
        f"stopped echoing requested model names (check tests/cli/test_chat.py).\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
