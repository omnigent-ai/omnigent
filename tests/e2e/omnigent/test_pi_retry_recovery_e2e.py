"""A pi turn survives a provider error that pi's automatic retry recovers from:
``omnigent run --harness pi -p ...`` against a provider that 503s once and then
answers must print the recovered answer and exit 0 instead of the 503 error."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.e2e._harness_probes import skip_if_harness_cli_missing
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, reset_mock_llm

# Served only by the mock's second response, i.e. after pi's retry; seeing it
# on stdout proves the turn survived the retry instead of ending at the 503.
_RECOVERED_SENTINEL = "PI_RETRY_RECOVERED_ANSWER"

# errorMessage pi classifies as retryable ("503"/"overloaded"), which drives
# the agent-level auto-retry the executor must not abandon.
_RETRYABLE_ERROR = "503 Service Unavailable: upstream overloaded, please retry"

_PROMPT = "Reply with the recovered answer."

# Harness startup + a 503 call + pi's retry backoff + the recovered call.
_RUN_TIMEOUT_SEC = 240


@pytest.mark.timeout(360)
def test_pi_turn_survives_provider_retry_recovery(
    tmp_path: Path,
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """A pi turn whose provider 503s once then recovers completes with the
    recovered answer, instead of being misreported as a hard 503 failure."""
    skip_if_harness_cli_missing("pi")

    mock_model = "mock-pi-retry-recovery"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"error": _RETRYABLE_ERROR, "status_code": 503},
            {"text": _RECOVERED_SENTINEL},
        ],
        key=mock_model,
    )

    yaml_path = tmp_path / "retry_recovery_agent.yaml"
    yaml_path.write_text(
        "name: retry_recovery_agent\nprompt: You are a helpful assistant.\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--model",
            mock_model,
            "--harness",
            "pi",
            "-p",
            _PROMPT,
            "--no-log",
            "--no-session",
        ],
        env=dict(mock_credentials_env),
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )

    request_count = len(get_mock_requests(mock_llm_server_url))

    # pi must actually reach its retry (a second provider call). One call means
    # the turn was torn down before pi could recover.
    assert request_count >= 2, (
        f"pi made only {request_count} provider call(s); the turn returned "
        f"before the auto-retry ran.\n\nstdout:\n{result.stdout!r}\n\n"
        f"stderr:\n{result.stderr!r}"
    )
    assert result.returncode == 0, (
        f"omnigent run exited {result.returncode}: the recovered turn was "
        f"misreported as a failure instead of completing.\n\n"
        f"stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assert _RECOVERED_SENTINEL in result.stdout, (
        f"Recovered answer {_RECOVERED_SENTINEL!r} not in stdout; the pi "
        f"executor returned at the first agent_end(willRetry=true) and dropped "
        f"pi's recovered answer.\n\nstdout:\n{result.stdout!r}\n\n"
        f"stderr:\n{result.stderr!r}"
    )
