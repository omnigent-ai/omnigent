"""The provider bearer-helper wrapper and the failure card it feeds.

Codex reports a failed ``auth.command`` as a bare ``sh`` exit status. The
wrapper keeps what the helper printed (redacted, bounded) in the bridge
directory, and the diagnosis names the provider/profile and the recovery step
for that provider instead of ``codex login``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native.bridge import (
    clear_bridge_state,
    codex_home_for_bridge_dir,
    read_provider_auth_failure,
    record_provider_auth_failure,
)
from omnigent.harnesses.codex_native.provider_auth import (
    OUTCOME_EMPTY_TOKEN,
    OUTCOME_EXITED,
    OUTCOME_TIMED_OUT,
    ProviderAuthFailure,
    ProviderAuthHelper,
    codex_message_outcome,
    databricks_cli_selector,
    diagnose_provider_auth_failure,
    is_provider_auth_error,
    is_static_key_command,
    wrap_provider_auth_command,
)

_UNAUTHENTICATED = json.dumps(
    {
        "error_code": "UNAUTHENTICATED",
        "message": "the refresh token is invalid; run `databricks auth login --profile oss`",
    }
)
_OSS_HELPER = ProviderAuthHelper(provider="DatabricksOSS", databricks_profile="oss")
_CODEX_EXIT_1 = "provider auth command `sh` exited with status exit status: 1"
# The wrapper runs under the host's PATH, as Codex's helper does; the bridge dir
# comes only from the CODEX_HOME each test chooses.
_HOST_PATH = os.environ.get("PATH", "/usr/bin:/bin")


def _run_wrapped(
    bridge_dir: Path,
    auth_command: str,
    helper: ProviderAuthHelper,
    *,
    timeout_ms: int = 15_000,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run *auth_command* through the wrapper exactly as Codex does: ``sh -c``."""
    codex_home = codex_home_for_bridge_dir(bridge_dir)
    codex_home.mkdir(parents=True, exist_ok=True)
    wrapped = wrap_provider_auth_command(auth_command, helper, timeout_ms=timeout_ms)
    return subprocess.run(
        ["sh", "-c", wrapped],
        capture_output=True,
        timeout=60,
        env={"PATH": _HOST_PATH, "CODEX_HOME": str(codex_home)},
        cwd=cwd,
        check=False,
    )


def test_wrapped_helper_passes_a_minted_token_through(tmp_path: Path) -> None:
    """The wrapper is transparent on success: the token reaches Codex, nothing is recorded."""
    record_provider_auth_failure(
        tmp_path,
        ProviderAuthFailure(_OSS_HELPER, OUTCOME_EXITED, 1, "null", ""),
    )

    result = _run_wrapped(tmp_path, "printf '%s\\n' eyJ-fake-token-value", _OSS_HELPER)

    assert result.returncode == 0
    assert result.stdout == b"eyJ-fake-token-value\n"
    # A successful mint clears the previous failure so the next error is not blamed on it.
    assert read_provider_auth_failure(tmp_path) is None


@pytest.mark.skipif(shutil.which("jq") is None, reason="the reported helper pipes through jq")
def test_wrapped_helper_records_the_null_a_jq_filter_leaves(tmp_path: Path) -> None:
    """The reported shape: the CLI's JSON error becomes ``null`` + exit 1 under ``jq -er``."""
    helper = f"printf '%s' {shlex.quote(_UNAUTHENTICATED)} | jq -er .access_token"

    result = _run_wrapped(tmp_path, helper, _OSS_HELPER)

    assert result.returncode == 1
    failure = read_provider_auth_failure(tmp_path)
    assert failure is not None
    assert failure.helper == _OSS_HELPER
    assert failure.outcome == OUTCOME_EXITED
    assert failure.exit_status == 1
    assert failure.stdout == "null"
    assert failure.stderr == ""


def test_wrapped_helper_records_structured_stdout_and_stderr(tmp_path: Path) -> None:
    """A helper that passes the CLI's JSON through keeps its error code and message."""
    helper = (
        f"printf '%s' {shlex.quote(_UNAUTHENTICATED)}; echo 'Error: not logged in' >&2; exit 3"
    )

    result = _run_wrapped(tmp_path, helper, _OSS_HELPER)

    assert result.returncode == 3
    assert result.stderr == b"Error: not logged in\n"
    failure = read_provider_auth_failure(tmp_path)
    assert failure is not None
    assert failure.exit_status == 3
    assert failure.reported_error() == (
        "UNAUTHENTICATED",
        "the refresh token is invalid; run `databricks auth login --profile oss`",
    )
    assert failure.stderr == "Error: not logged in"


def test_wrapped_helper_redacts_credentials_inside_a_json_error_message(tmp_path: Path) -> None:
    """A retained JSON diagnostic keeps its words but not an opaque value, digits or not."""
    secret = "abcdefghijklmnopqrstuvwxyzabcdefghij"
    payload = json.dumps(
        {"error_code": "UNAUTHENTICATED", "message": f"token {secret} was rejected"}
    )

    result = _run_wrapped(tmp_path, f"printf '%s' {shlex.quote(payload)}; exit 1", _OSS_HELPER)

    assert result.returncode == 1
    failure = read_provider_auth_failure(tmp_path)
    assert failure is not None
    assert secret not in failure.stdout
    assert failure.reported_error() == ("UNAUTHENTICATED", "token [REDACTED] was rejected")
    assert secret not in diagnose_provider_auth_failure(_CODEX_EXIT_1, failure).cause


def test_helper_metadata_is_credential_free_and_bounded(tmp_path: Path) -> None:
    """A --host URL with userinfo or a query never reaches the record, card or message."""
    helper = ProviderAuthHelper(
        provider="p" * 300,
        databricks_host="https://alice:s3cretpass@ws.example.com:8443/api?token=querysecret#f",
    )
    assert helper.databricks_host == "https://ws.example.com:8443"
    assert len(helper.provider) <= 121

    result = _run_wrapped(tmp_path, "echo 'Error: not logged in' >&2; exit 1", helper)

    assert result.returncode == 1
    record = (tmp_path / "provider_auth_failure.json").read_text(encoding="utf-8")
    failure = read_provider_auth_failure(tmp_path)
    assert failure is not None
    diagnosis = diagnose_provider_auth_failure(_CODEX_EXIT_1, failure)
    for secret in ("s3cretpass", "querysecret", "alice"):
        assert secret not in record
        assert secret not in diagnosis.cause
        assert secret not in diagnosis.remediation
        assert secret not in diagnosis.message(_CODEX_EXIT_1)
    assert "`databricks auth login --host https://ws.example.com:8443`" in diagnosis.remediation


def test_wrapped_helper_records_an_empty_token(tmp_path: Path) -> None:
    """Exit 0 with no token (the generated Databricks helper's failure shape) is recorded too."""
    result = _run_wrapped(
        tmp_path,
        "echo 'Error: token refresh failed' >&2; printf '\\n'",
        ProviderAuthHelper(provider="Databricks", databricks_profile="oss"),
    )

    assert result.returncode == 0
    assert result.stdout.strip() == b""
    failure = read_provider_auth_failure(tmp_path)
    assert failure is not None
    assert failure.outcome == OUTCOME_EMPTY_TOKEN
    assert failure.stderr == "Error: token refresh failed"


def test_wrapped_helper_redacts_and_bounds_what_it_records(tmp_path: Path) -> None:
    """Labeled credentials on stderr are redacted and free-form stdout is not kept at all."""
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlc2lnbmF0dXJl"
    helper = (
        f"echo 'access_token: {jwt}' >&2; echo 'Authorization: Bearer dapi{'a' * 40}' >&2; "
        "head -c 6000 /dev/zero | tr '\\0' x; exit 1"
    )

    result = _run_wrapped(tmp_path, helper, _OSS_HELPER)

    assert result.returncode == 1
    failure = read_provider_auth_failure(tmp_path)
    assert failure is not None
    assert jwt not in failure.stderr
    assert "dapi" + "a" * 40 not in failure.stderr
    assert failure.stderr.count("[REDACTED]") == 2
    assert failure.stdout == "[output omitted: 6000 bytes]"


def test_wrapped_helper_never_keeps_token_like_output(tmp_path: Path) -> None:
    """A helper that prints a bare token and then fails leaks nothing to the record or card."""
    token = "9f8e7d6c5b4a39281706f5e4d3c2b1a0f1e2d3c4"
    helper = (
        f"printf '%s' {token}; echo 'warn: refreshing {token} failed' >&2; "
        "echo 'Error: token refresh failed' >&2; exit 1"
    )

    result = _run_wrapped(tmp_path, helper, _OSS_HELPER)

    assert result.returncode == 1
    failure = read_provider_auth_failure(tmp_path)
    assert failure is not None
    assert failure.stdout == f"[output omitted: {len(token)} bytes]"
    assert token not in failure.stderr
    assert failure.stderr == "warn: refreshing [REDACTED] failed\nError: token refresh failed"
    diagnosis = diagnose_provider_auth_failure(_CODEX_EXIT_1, failure)
    assert token not in diagnosis.cause
    assert "Error: token refresh failed" in diagnosis.cause


def test_wrapped_helper_ignores_a_workspace_package_named_omnigent(tmp_path: Path) -> None:
    """Codex runs the helper in the session workspace; a checkout there must not shadow it."""
    workspace = tmp_path / "workspace"
    (workspace / "omnigent").mkdir(parents=True)
    (workspace / "omnigent" / "__init__.py").write_text(
        'raise RuntimeError("workspace package shadowed the installed omnigent")\n'
    )
    bridge_dir = tmp_path / "bridge"

    result = _run_wrapped(
        bridge_dir, "printf '%s\\n' eyJ-fake-token-value", _OSS_HELPER, cwd=workspace
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == b"eyJ-fake-token-value\n"
    assert read_provider_auth_failure(bridge_dir) is None


@pytest.mark.parametrize("timeout_ms", [2_000, 600], ids=["normal-budget", "tiny-budget"])
def test_wrapped_helper_times_out_before_codex_and_records_it(
    tmp_path: Path, timeout_ms: int
) -> None:
    """A hung helper is ended, with its whole process group, inside Codex's budget (even one
    smaller than the usual margin), and the record still yields the timeout card once Codex
    words the wrapper's exit 124 as an exit status."""
    result = _run_wrapped(tmp_path, "sleep 30", _OSS_HELPER, timeout_ms=timeout_ms)

    assert result.returncode == 124
    failure = read_provider_auth_failure(tmp_path)
    assert failure is not None
    assert failure.outcome == OUTCOME_TIMED_OUT
    assert failure.exit_status is None
    codex_message = (
        f"provider auth command `sh` exited with status exit status: {result.returncode}"
    )
    diagnosis = diagnose_provider_auth_failure(codex_message, failure)
    assert diagnosis.used_record is True
    assert "provider 'DatabricksOSS' (Databricks profile 'oss')" in diagnosis.cause
    assert "did not finish in time" in diagnosis.cause
    assert "`databricks auth login --profile oss`" in diagnosis.remediation


def test_helper_is_left_alone_without_an_interpreter_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an interpreter path the helper is left bare; python3 cannot import omnigent."""
    monkeypatch.setattr(sys, "executable", "")

    assert wrap_provider_auth_command("my-helper token", _OSS_HELPER, timeout_ms=15_000) == (
        "my-helper token"
    )


def test_wrapped_helper_without_a_bridge_home_only_passes_through(tmp_path: Path) -> None:
    """Outside an Omnigent session (no bridge ``CODEX_HOME``) nothing is written anywhere."""
    wrapped = wrap_provider_auth_command("echo oops >&2; exit 2", _OSS_HELPER, timeout_ms=15_000)
    result = subprocess.run(
        ["sh", "-c", wrapped],
        capture_output=True,
        timeout=60,
        env={"PATH": _HOST_PATH, "CODEX_HOME": str(tmp_path / "elsewhere")},
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr == b"oops\n"
    assert list(tmp_path.iterdir()) == []


def test_wrapped_command_names_the_interpreter_and_keeps_the_helper_verbatim() -> None:
    """The wrapper runs with the runner's own interpreter and quotes the helper as one argument."""
    helper = 'databricks auth token --profile oss --output json | jq -er ".access_token"'
    wrapped = wrap_provider_auth_command(
        helper,
        ProviderAuthHelper(provider="DatabricksOSS", databricks_profile="oss"),
        timeout_ms=15_000,
    )

    words = shlex.split(wrapped)
    assert words[:4] == [
        sys.executable,
        "-I",
        "-m",
        "omnigent.harnesses.codex_native.provider_auth",
    ]
    assert words[-1] == helper
    assert words[words.index("--provider") + 1] == "DatabricksOSS"
    assert words[words.index("--databricks-profile") + 1] == "oss"
    assert "--databricks-host" not in words


@pytest.mark.parametrize(
    ("auth_command", "expected"),
    [
        (
            "databricks auth token --profile oss --output json | jq -er .access_token",
            ("oss", None),
        ),
        ("databricks auth token -p dev --output json | jq -r .access_token", ("dev", None)),
        ("databricks auth token --profile=qa | jq -r .access_token", ("qa", None)),
        (
            "env -u DATABRICKS_CONFIG_PROFILE databricks auth token --host https://x.databricks.com",
            (None, "https://x.databricks.com"),
        ),
        # The parsed host is raw here; ProviderAuthHelper strips credentials from it.
        (
            "databricks auth token --host https://u:p@x.databricks.com/?t=1 | jq -r .access_token",
            (None, "https://u:p@x.databricks.com/?t=1"),
        ),
        ("databricks auth token | jq -r .access_token --profile stray", (None, None)),
        # A flag or separator where the value should be is not a profile.
        ("databricks auth token --profile --output json | jq -er .access_token", (None, None)),
        ("databricks auth token --profile | jq -r .access_token", (None, None)),
        # Operators glued to the value or the next word still end the CLI's arguments.
        ("databricks auth token --profile oss; jq -r .access_token", ("oss", None)),
        ("databricks auth token --profile oss|jq -r .access_token", ("oss", None)),
        ("databricks auth token --profile oss --output json|jq -er .access_token", ("oss", None)),
        ("printf %s sk-static", (None, None)),
        ("databricks auth login --profile oss", (None, None)),
        ("unbalanced 'quote databricks auth token --profile oss", (None, None)),
    ],
)
def test_databricks_cli_selector(
    auth_command: str, expected: tuple[str | None, str | None]
) -> None:
    assert databricks_cli_selector(auth_command) == expected


@pytest.mark.parametrize(
    "message",
    [
        _CODEX_EXIT_1,
        "provider auth command `sh` exited with status exit status: 1: Error: not logged in",
        "provider auth command `sh` produced an empty token",
        "provider auth command `sh` timed out after 15000 ms",
    ],
)
def test_codex_provider_auth_messages_are_recognized(message: str) -> None:
    assert is_provider_auth_error(message)


@pytest.mark.parametrize(
    "message", ["401 Unauthorized", "disk full", "Please run codex login", "stream disconnected"]
)
def test_other_codex_errors_are_not_provider_auth(message: str) -> None:
    assert not is_provider_auth_error(message)


def test_diagnosis_for_the_reported_incident_names_profile_and_databricks_login() -> None:
    """``null`` + exit 1 from ``oss``: name the profile and the lost error; no ``codex login``."""
    failure = ProviderAuthFailure(_OSS_HELPER, OUTCOME_EXITED, 1, "null", "")

    diagnosis = diagnose_provider_auth_failure(_CODEX_EXIT_1, failure)

    assert diagnosis.title == "Codex could not get a Databricks token"
    assert "provider 'DatabricksOSS' (Databricks profile 'oss')" in diagnosis.cause
    assert "exited with status 1" in diagnosis.cause
    assert "printed `null` instead of a token" in diagnosis.cause
    assert "`databricks auth token --profile oss --output json`" in diagnosis.remediation
    assert "`databricks auth login --profile oss`" in diagnosis.remediation
    assert diagnosis.login_required is False
    message = diagnosis.message(_CODEX_EXIT_1)
    assert message.startswith(diagnosis.cause)
    assert f"Try this: {diagnosis.remediation}" in message
    assert message.endswith(f"Codex reported: {_CODEX_EXIT_1}")
    assert "codex login" not in message


def test_diagnosis_quotes_only_the_first_line_of_a_long_report() -> None:
    """The structured cause stays a sentence or two; the record keeps the full report."""
    report = "Error: " + "x" * 300 + "\nsecond line"
    failure = ProviderAuthFailure(_OSS_HELPER, OUTCOME_EXITED, 1, "", report)

    diagnosis = diagnose_provider_auth_failure(_CODEX_EXIT_1, failure)

    assert "it reported: Error: " + "x" * 113 + "…" in diagnosis.cause
    assert "second line" not in diagnosis.cause
    assert len(diagnosis.cause) < 256
    assert failure.stderr == report


def test_diagnosis_surfaces_a_structured_unauthenticated_error_as_login_required() -> None:
    failure = ProviderAuthFailure(_OSS_HELPER, OUTCOME_EXITED, 1, _UNAUTHENTICATED, "")

    diagnosis = diagnose_provider_auth_failure(_CODEX_EXIT_1, failure)

    assert diagnosis.login_required is True
    assert diagnosis.title == "Databricks sign-in required for Codex"
    assert "the Databricks CLI reported UNAUTHENTICATED: the refresh token is invalid" in (
        diagnosis.cause
    )
    assert diagnosis.remediation.startswith(
        "Sign in to Databricks again on the host that runs this agent: "
        "`databricks auth login --profile oss`."
    )


def test_diagnosis_for_an_ordinary_stderr_failure_does_not_assume_a_login() -> None:
    failure = ProviderAuthFailure(
        _OSS_HELPER, OUTCOME_EXITED, 127, "", "sh: 1: databricks: not found"
    )

    diagnosis = diagnose_provider_auth_failure(f"{_CODEX_EXIT_1}27", failure)

    assert diagnosis.login_required is False
    assert "exited with status 127; it reported: sh: 1: databricks: not found" in diagnosis.cause
    assert diagnosis.remediation.startswith("On the host that runs this agent, run")
    assert "codex login" not in diagnosis.remediation


def test_diagnosis_for_the_generated_databricks_helper_uses_its_stderr() -> None:
    """The ``databricks`` kind prints nothing on failure; its CLI stderr carries the cause."""
    failure = ProviderAuthFailure(
        ProviderAuthHelper(provider="Databricks", databricks_profile="oss"),
        OUTCOME_EMPTY_TOKEN,
        0,
        "",
        "Error: the refresh token is invalid, please run `databricks auth login --profile oss`",
    )

    diagnosis = diagnose_provider_auth_failure(
        "provider auth command `sh` produced an empty token", failure
    )

    assert diagnosis.login_required is True
    assert "printed no token; it reported: Error: the refresh token is invalid" in diagnosis.cause
    assert "`databricks auth login --profile oss`" in diagnosis.remediation


def test_diagnosis_for_a_timeout_points_at_a_waiting_browser_login() -> None:
    failure = ProviderAuthFailure(_OSS_HELPER, OUTCOME_TIMED_OUT, None, "", "")

    diagnosis = diagnose_provider_auth_failure(
        "provider auth command `sh` timed out after 15000 ms", failure
    )

    assert "did not finish in time" in diagnosis.cause
    assert "waiting for a browser sign-in" in diagnosis.remediation
    assert "`databricks auth login --profile oss`" in diagnosis.remediation


def test_diagnosis_for_a_host_selected_helper_signs_in_by_host() -> None:
    failure = ProviderAuthFailure(
        ProviderAuthHelper(provider="Gateway", databricks_host="https://x.databricks.com"),
        OUTCOME_EXITED,
        1,
        "",
        "not logged in",
    )

    diagnosis = diagnose_provider_auth_failure(_CODEX_EXIT_1, failure)

    assert "Databricks workspace https://x.databricks.com" in diagnosis.cause
    assert "`databricks auth login --host https://x.databricks.com`" in diagnosis.remediation


def test_diagnosis_for_a_non_databricks_helper_points_at_the_provider_setup() -> None:
    failure = ProviderAuthFailure(
        ProviderAuthHelper(provider="OpenRouter"), OUTCOME_EXITED, 1, "", "vault: sealed"
    )

    diagnosis = diagnose_provider_auth_failure(_CODEX_EXIT_1, failure)

    assert diagnosis.title == "Codex could not get a provider token"
    assert (
        "provider 'OpenRouter' exited with status 1; it reported: vault: sealed" in diagnosis.cause
    )
    assert "the auth command for provider 'OpenRouter'" in diagnosis.remediation
    assert "setup`" in diagnosis.remediation
    assert "databricks" not in diagnosis.remediation
    assert "codex login" not in diagnosis.remediation


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (_CODEX_EXIT_1, OUTCOME_EXITED),
        ("provider auth command `sh` produced an empty token", OUTCOME_EMPTY_TOKEN),
        ("provider auth command `sh` timed out after 15000 ms", OUTCOME_TIMED_OUT),
        # The wrapper ends a hung helper itself and exits 124 before Codex's timeout.
        ("provider auth command `sh` exited with status exit status: 124", OUTCOME_TIMED_OUT),
        # Codex's own wording wins over a helper stderr that mentions a timeout.
        (
            _CODEX_EXIT_1 + ": Error: refresh timed out",
            OUTCOME_EXITED,
        ),
        ("provider auth command `sh` could not be spawned", None),
    ],
)
def test_codex_message_outcome(message: str, expected: str | None) -> None:
    assert codex_message_outcome(message) == expected


def test_diagnosis_keeps_a_helper_that_itself_exited_124() -> None:
    """A helper exiting 124 on its own is recorded as an exit yet reads as a timeout to Codex."""
    failure = ProviderAuthFailure(_OSS_HELPER, OUTCOME_EXITED, 124, "", "timeout: databricks")

    diagnosis = diagnose_provider_auth_failure(
        "provider auth command `sh` exited with status exit status: 124: timeout: databricks",
        failure,
    )

    assert diagnosis.used_record is True
    assert "exited with status 124; it reported: timeout: databricks" in diagnosis.cause


def test_diagnosis_redacts_a_token_codex_appended_from_the_helper_stderr() -> None:
    """Codex appends the helper's raw stderr to its message; the card must not echo a token."""
    token = "9f8e7d6c5b4a39281706f5e4d3c2b1a0f1e2d3c4"
    codex_message = f"{_CODEX_EXIT_1}: refreshing {token} failed"

    without_record = diagnose_provider_auth_failure(codex_message, None)
    with_record = diagnose_provider_auth_failure(
        codex_message, ProviderAuthFailure(_OSS_HELPER, OUTCOME_EXITED, 1, "null", "")
    )

    assert token not in without_record.cause
    assert token not in without_record.message(codex_message)
    assert token not in with_record.message(codex_message)
    assert "[REDACTED]" in with_record.message(codex_message)


def test_diagnosis_ignores_a_record_from_a_different_failure() -> None:
    """A record left by an earlier timeout must not describe a later exit-status failure."""
    stale = ProviderAuthFailure(_OSS_HELPER, OUTCOME_TIMED_OUT, None, "", "")

    diagnosis = diagnose_provider_auth_failure(_CODEX_EXIT_1, stale)

    assert diagnosis.used_record is False
    assert diagnosis.title == "Codex could not get a provider token"
    assert "did not finish in time" not in diagnosis.cause
    assert _CODEX_EXIT_1 in diagnosis.cause


def test_diagnosis_without_a_record_uses_codex_message_and_never_codex_login() -> None:
    message = "provider auth command `sh` exited with status exit status: 1: Please log in first"

    diagnosis = diagnose_provider_auth_failure(message, None)

    assert diagnosis.title == "Codex could not get a provider token"
    assert diagnosis.cause.endswith(message + ".") or message in diagnosis.cause
    assert diagnosis.login_required is True
    assert "sign in to the provider again" in diagnosis.remediation
    assert "codex login" not in diagnosis.remediation


def test_record_round_trips_and_rejects_malformed_records(tmp_path: Path) -> None:
    failure = ProviderAuthFailure(
        ProviderAuthHelper(provider="Gateway", databricks_host="https://x.databricks.com"),
        OUTCOME_TIMED_OUT,
        None,
        "",
        "tail",
    )
    record_provider_auth_failure(tmp_path, failure)
    assert read_provider_auth_failure(tmp_path) == failure

    (tmp_path / "provider_auth_failure.json").write_text('{"outcome": "weird"}', encoding="utf-8")
    assert read_provider_auth_failure(tmp_path) is None
    # A boolean is not an exit status.
    (tmp_path / "provider_auth_failure.json").write_text(
        '{"provider": "p", "outcome": "exited", "exit_status": true}', encoding="utf-8"
    )
    malformed = read_provider_auth_failure(tmp_path)
    assert malformed is not None
    assert malformed.exit_status is None
    (tmp_path / "provider_auth_failure.json").write_text("not json", encoding="utf-8")
    assert read_provider_auth_failure(tmp_path) is None


def test_relaunch_clears_a_recorded_helper_failure(tmp_path: Path) -> None:
    """A new app-server must not inherit the previous launch's helper failure."""
    record_provider_auth_failure(
        tmp_path, ProviderAuthFailure(_OSS_HELPER, OUTCOME_EXITED, 1, "null", "")
    )
    assert read_provider_auth_failure(tmp_path) is not None

    clear_bridge_state(tmp_path)

    assert read_provider_auth_failure(tmp_path) is None


@pytest.mark.parametrize(
    ("auth_command", "expected"),
    [
        ("printf %s sk-static", True),
        ("printf '%s' sk-static", True),
        # A quoted literal dollar is still static; only an expansion can fail.
        (f"printf %s {shlex.quote('test-$KEY_FRAGMENT-' + chr(39) + 'quoted' + chr(39))}", True),
        ('printf %s "$TOKEN"', False),
        ("printf %s `cat key`", False),
        ("printf %s $(cat key)", False),
        ("printf %s", False),
        ("databricks auth token --profile oss | jq -r .access_token", False),
    ],
)
def test_is_static_key_command(auth_command: str, expected: bool) -> None:
    """Only a literal ``printf %s <key>`` skips the recorder; an expanded key can still fail."""
    assert is_static_key_command(auth_command) is expected
