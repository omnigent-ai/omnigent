"""Unit tests for recognizing the cause of a native terminal start failure.

A start failure's exception text can carry paths or secrets, so recognition may
return only a fixed reason token from a closed set, never any of the text.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import httpx
import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner import terminal as terminal_mod
from omnigent.runner.native import start_failure
from omnigent.runner.native.start_failure import classify_start_failure

_INSTALL_ERROR = (
    "Error: Missing optional dependency @openai/codex-linux-x64. "
    "Reinstall Codex: npm install -g @openai/codex@latest"
)
_NODE_FRAME = (
    "    at Object.<anonymous> (/home/alice/.nvm/node_modules/@openai/codex/bin/codex.js:79)"
)
_TIMED_OUT = "Timed out after 60s waiting for the Codex app-server at ws://127.0.0.1:50000"

# (case id, expected reason, the exception text a real raise site produces)
_RECOGNIZED: list[tuple[str, str, BaseException]] = [
    (
        "install-incomplete",
        "codex_install_incomplete",
        RuntimeError(f"Codex app-server exited early: {_INSTALL_ERROR} | {_NODE_FRAME}"),
    ),
    (
        "config-legacy-profile",
        "codex_config_rejected",
        RuntimeError(
            'Codex app-server exited early: Error: legacy `profile = "ucode"` config '
            "is no longer supported"
        ),
    ),
    (
        "config-model-provider-not-found",
        "codex_config_rejected",
        RuntimeError(
            "Codex app-server exited early: Error: Model provider `Databricks` not found"
        ),
    ),
    (
        "config-profile-not-found",
        "codex_config_rejected",
        RuntimeError("Codex app-server exited early: Error: config profile `ucode` not found"),
    ),
    (
        "config-dashed-model-provider",
        "codex_config_rejected",
        RuntimeError(
            "Codex app-server exited early: Error: Model provider `ucode-databricks` not found"
        ),
    ),
    (
        "config-toml-parse-error",
        "codex_config_rejected",
        RuntimeError(
            "Codex app-server exited early: Error loading config.toml: "
            "TOML parse error at line 3, column 1"
        ),
    ),
    (
        "config-invalid-configuration-in-timeout",
        "codex_config_rejected",
        RuntimeError(
            f"{_TIMED_OUT}: [Errno 111] Connect call failed; "
            "stderr=Invalid configuration; using defaults."
        ),
    ),
    ("tmux-missing", "tmux_missing", RuntimeError("tmux is not installed or not on PATH")),
    (
        "tmux-too-old",
        "tmux_unsupported",
        RuntimeError("tmux 3.2 is too old; managed terminals require tmux 3.3 or newer"),
    ),
    (
        "tmux-unknown-version",
        "tmux_unsupported",
        RuntimeError(
            "Could not determine the installed tmux version; managed terminals require "
            "tmux 3.3 or newer"
        ),
    ),
    (
        "cli-codex",
        "cli_not_found",
        ImportError("Native Codex requires the 'codex' CLI on PATH. Set OMNIGENT_CODEX_PATH."),
    ),
    (
        "cli-pi",
        "cli_not_found",
        click.ClickException("Native Pi requires the 'pi' CLI on PATH. Install Pi."),
    ),
    (
        "cli-opencode",
        "cli_not_found",
        RuntimeError("opencode CLI not found on PATH; install the 'opencode-ai' npm package"),
    ),
    (
        "cli-agy",
        "cli_not_found",
        RuntimeError("agy CLI not found on PATH and not at /home/alice/.local/bin/agy."),
    ),
    (
        "app-server-timeout",
        "app_server_start_timeout",
        RuntimeError(
            f"{_TIMED_OUT}: [Errno 111] Connect call failed ('127.0.0.1', 50000); stderr="
        ),
    ),
    (
        "app-server-exited-early",
        "app_server_exited_early",
        RuntimeError("Codex app-server exited early: thread 'main' panicked"),
    ),
]


@pytest.mark.parametrize(
    ("reason", "exc"),
    [pytest.param(reason, exc, id=case_id) for case_id, reason, exc in _RECOGNIZED],
)
def test_known_cause_is_named_by_its_reason_token(reason: str, exc: BaseException) -> None:
    cause = classify_start_failure(exc)

    assert cause is not None
    assert cause.reason == reason


def test_every_reason_token_has_a_recognition_case() -> None:
    """A token cannot be added to the table without a case proving it matches."""
    tested = {reason for _, reason, _ in _RECOGNIZED}
    assert tested == {matcher.cause.reason for matcher in start_failure._MATCHERS}


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(RuntimeError("token=sk-secret /home/alice/private boom"), id="free-text"),
        pytest.param(RuntimeError(), id="empty"),
        pytest.param(OSError(28, "No space left on device"), id="disk-full"),
        pytest.param(FileNotFoundError(2, "No such file or directory"), id="cwd-deleted"),
        pytest.param(
            OmnigentError("something else broke", code=ErrorCode.INTERNAL_ERROR), id="omnigent"
        ),
        pytest.param(httpx.ReadTimeout("slow"), id="transport"),
        pytest.param(click.ClickException("Pi session creation failed"), id="other-click"),
        pytest.param(RuntimeError("tmux launch failed (rc=1): no server"), id="tmux-launch"),
        pytest.param(
            RuntimeError("Invalid configuration: agent spec has no name"), id="config-not-codex"
        ),
        pytest.param(
            RuntimeError(
                "{'code': -32603, 'message': 'thread-store internal error: "
                "Model provider `` not found'}"
            ),
            id="provider-not-found-outside-readiness",
        ),
    ],
)
def test_unknown_failure_has_no_reason(exc: BaseException) -> None:
    assert classify_start_failure(exc) is None


def test_install_cause_wins_over_the_generic_readiness_symptoms() -> None:
    """A readiness error quotes stderr, so the cause beats "exited early" and "timed out"."""
    exited = RuntimeError(f"Codex app-server exited early: {_INSTALL_ERROR}")
    timed_out = RuntimeError(f"{_TIMED_OUT}: None; stderr={_INSTALL_ERROR}")

    for exc in (exited, timed_out):
        cause = classify_start_failure(exc)
        assert cause is not None
        assert cause.reason == "codex_install_incomplete"


def test_wrapped_cause_is_recognized_through_a_short_chain() -> None:
    outer = RuntimeError("terminal setup failed")
    outer.__cause__ = RuntimeError("tmux is not installed or not on PATH")

    cause = classify_start_failure(outer)

    assert cause is not None
    assert cause.reason == "tmux_missing"


def _wrapped_with_context(*, suppressed: bool) -> RuntimeError:
    """Build a wrapper raised in an ``except`` block; ``from None`` suppresses the context."""
    wrapper = RuntimeError("terminal setup failed")
    wrapper.__context__ = RuntimeError("tmux is not installed or not on PATH")
    wrapper.__suppress_context__ = suppressed
    return wrapper


def test_implicit_context_is_searched() -> None:
    cause = classify_start_failure(_wrapped_with_context(suppressed=False))

    assert cause is not None
    assert cause.reason == "tmux_missing"


def test_suppressed_context_is_not_searched() -> None:
    assert classify_start_failure(_wrapped_with_context(suppressed=True)) is None


def test_cause_beyond_the_chain_depth_is_ignored() -> None:
    exc: BaseException = RuntimeError("tmux is not installed or not on PATH")
    for _ in range(start_failure._CHAIN_MAX_DEPTH):
        wrapper = RuntimeError("wrapper")
        wrapper.__cause__ = exc
        exc = wrapper

    assert classify_start_failure(exc) is None


def test_exception_with_a_failing_str_is_skipped_not_raised() -> None:
    class _Hostile(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no text for you")

    hostile = _Hostile()
    hostile.__cause__ = RuntimeError("tmux is not installed or not on PATH")

    cause = classify_start_failure(hostile)

    assert cause is not None
    assert cause.reason == "tmux_missing"
    assert classify_start_failure(_Hostile()) is None


def test_result_never_carries_the_matched_text() -> None:
    secret = "sk-abcdef0123456789"
    exc = RuntimeError(
        f"Codex app-server exited early: {_INSTALL_ERROR} token={secret} | {_NODE_FRAME}"
    )

    cause = classify_start_failure(exc)

    assert cause is not None
    assert secret not in repr(cause)
    assert "/home/alice" not in repr(cause)
    assert "Missing optional dependency" not in repr(cause)


def test_reason_tokens_are_a_closed_slug_vocabulary() -> None:
    reasons = [matcher.cause.reason for matcher in start_failure._MATCHERS]

    assert len(set(reasons)) == len(reasons)
    assert all(re.fullmatch(r"[a-z]+(?:_[a-z]+)*", reason) for reason in reasons)


def test_remedies_are_single_fixed_sentences() -> None:
    remedies = {m.cause.reason: m.cause.remedy for m in start_failure._MATCHERS}

    # A symptom with no single clear fix offers no remedy.
    assert remedies["app_server_start_timeout"] is None
    assert remedies["app_server_exited_early"] is None
    for reason, remedy in remedies.items():
        if remedy is not None:
            assert remedy.endswith("."), reason
            assert "\n" not in remedy and "{" not in remedy, reason
    assert "npm install -g @openai/codex@latest" in str(remedies["codex_install_incomplete"])
    assert "~/.codex/config.toml" in str(remedies["codex_config_rejected"])


# --- the real raise sites: these guard the matchers against message drift ----


def test_real_tmux_missing_error_is_recognized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal_mod.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError) as info:
        terminal_mod._require_supported_tmux()

    cause = classify_start_failure(info.value)
    assert cause is not None
    assert cause.reason == "tmux_missing"


@pytest.mark.parametrize("version", [(3, 2), None])
def test_real_unsupported_tmux_error_is_recognized(
    monkeypatch: pytest.MonkeyPatch, version: tuple[int, int] | None
) -> None:
    monkeypatch.setattr(terminal_mod.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(terminal_mod, "tmux_version", lambda _: version)
    with pytest.raises(RuntimeError) as info:
        terminal_mod._require_supported_tmux()

    cause = classify_start_failure(info.value)
    assert cause is not None
    assert cause.reason == "tmux_unsupported"


def test_real_missing_codex_cli_error_is_recognized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.harnesses.codex_native import app_server

    monkeypatch.setattr(app_server, "_find_codex_cli", lambda: None)
    with pytest.raises(ImportError) as info:
        app_server.build_codex_native_server(
            socket_path=tmp_path / "codex.sock",
            codex_home=tmp_path / "home",
            cwd=tmp_path,
            model=None,
            profile=None,
            bridge_dir=tmp_path,
        )

    cause = classify_start_failure(info.value)
    assert cause is not None
    assert cause.reason == "cli_not_found"


def test_real_missing_pi_cli_error_is_recognized(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.harnesses.pi_native import main as pi_main

    monkeypatch.setattr(pi_main, "resolve_cli_binary", lambda *_args, **_kwargs: None)
    with pytest.raises(click.ClickException) as info:
        pi_main.resolve_pi_executable(env={})

    cause = classify_start_failure(info.value)
    assert cause is not None
    assert cause.reason == "cli_not_found"
