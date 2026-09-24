"""Windows harness-readiness regressions."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import omnigent.onboarding.harness_install as hi
import omnigent.onboarding.harness_readiness as readiness
from omnigent.harness_aliases import NATIVE_HARNESSES


@pytest.fixture(autouse=True)
def _isolate_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    import omnigent.onboarding.copilot_auth as _ca

    monkeypatch.setattr(_ca, "gh_cli_github_token", lambda host=None: None)

    import omnigent._platform as _plat

    monkeypatch.delenv("OMNIGENT_CODEX_PATH", raising=False)
    monkeypatch.setattr(_plat, "_cli_fallback_dirs", lambda: ())


def _all_clis_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _stub_run(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            if argv[0].endswith("opencode"):
                version = "1.17.7\n"
            elif argv[0].endswith("cursor-agent") or argv[0].endswith("hermes"):
                version = "2026.07.01\n"
            else:
                version = "9.9.9\n"
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=version, stderr="")
        if argv[:3] == ["gh", "auth", "token"]:
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess in readiness tests: {argv!r}")

    monkeypatch.setattr(hi.subprocess, "run", _stub_run)
    monkeypatch.setattr(hi, "harness_cli_logged_in", lambda _key, **_kw: True)


@pytest.mark.parametrize("harness", sorted(NATIVE_HARNESSES))
def test_native_terminal_harness_unavailable_on_windows(
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
) -> None:
    _all_clis_installed(monkeypatch)
    import omnigent._platform as _plat

    monkeypatch.setattr(_plat, "IS_WINDOWS", True)
    monkeypatch.setattr(readiness, "IS_WINDOWS", True)

    result = readiness.configured_harness_map()

    assert result.get(harness) is not True


def test_sdk_harnesses_remain_available_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _all_clis_installed(monkeypatch)
    import omnigent._platform as _plat

    monkeypatch.setattr(_plat, "IS_WINDOWS", True)
    monkeypatch.setattr(readiness, "IS_WINDOWS", True)

    result = readiness.configured_harness_map()

    for sdk_harness in ("claude-sdk", "claude_sdk", "openai-agents", "openai-agents-sdk"):
        assert result.get(sdk_harness) is True
