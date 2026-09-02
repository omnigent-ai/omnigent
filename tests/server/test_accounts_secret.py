"""Regression tests for omnigent.server.accounts_secret.

Covers the load_or_generate_cookie_secret helper — specifically the
Windows-portability bug where os.fchmod is unavailable (Python's os
module only has fchmod on POSIX; Windows raises AttributeError).

Symptom: `omnigent server` crashes immediately on Windows with:
    AttributeError: module 'os' has no attribute 'fchmod'.
    Did you mean: 'chmod'?
"""

from __future__ import annotations

import importlib
import os
import stat
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_module():
    """Re-import accounts_secret so the module's top-level os reference
    reflects any monkeypatches already applied to the os module.
    """
    import omnigent.server.accounts_secret as mod

    importlib.reload(mod)
    return mod


# ---------------------------------------------------------------------------
# Happy-path (POSIX + env-var) tests
# ---------------------------------------------------------------------------


def test_env_var_wins_without_touching_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OMNIGENT_ACCOUNTS_COOKIE_SECRET in env is returned as-is, no file I/O.

    On every platform — including Windows — the env-var path must work
    without calling os.open / os.fchmod, because the function returns before
    any file access.
    """
    expected = "a" * 64
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", expected)

    import omnigent.server.accounts_secret as mod

    result = mod.load_or_generate_cookie_secret(tmp_path)

    assert result == expected
    # No secret file should have been created.
    assert not (tmp_path / "accounts-cookie-secret").exists()


def test_generates_secret_on_first_boot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A new data-dir results in a fresh 64-char hex secret persisted to disk."""
    monkeypatch.delenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", raising=False)

    import omnigent.server.accounts_secret as mod

    result = mod.load_or_generate_cookie_secret(tmp_path)

    assert isinstance(result, str)
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)

    secret_file = tmp_path / "accounts-cookie-secret"
    assert secret_file.exists()
    assert secret_file.read_text().strip() == result


def test_reads_existing_secret_on_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A pre-existing secret file is reused — sessions survive restarts."""
    monkeypatch.delenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", raising=False)

    secret_file = tmp_path / "accounts-cookie-secret"
    persisted = "b" * 64
    secret_file.write_text(persisted + "\n")

    import omnigent.server.accounts_secret as mod

    result = mod.load_or_generate_cookie_secret(tmp_path)

    assert result == persisted


# ---------------------------------------------------------------------------
# Windows-portability regression: os.fchmod absent
# ---------------------------------------------------------------------------


def test_no_crash_when_fchmod_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """load_or_generate_cookie_secret must not crash when os.fchmod is absent.

    Windows does not provide os.fchmod; calling it raises:
        AttributeError: module 'os' has no attribute 'fchmod'
    This test simulates that platform by removing fchmod from the os module
    and asserts the function still returns a valid secret.
    """
    monkeypatch.delenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", raising=False)

    # monkeypatch.delattr removes the attribute and restores it after the test,
    # faithfully simulating a Windows Python where os.fchmod is absent.
    if hasattr(os, "fchmod"):
        monkeypatch.delattr(os, "fchmod")

    # Reload the module so it picks up the patched os (the module captured
    # `import os` at load time, but uses `os.fchmod` by attribute lookup at
    # call time — so reloading is not strictly necessary, but ensures the
    # module's internal reference matches the patched state).
    mod = _reload_module()

    # This must NOT raise AttributeError (or any other exception).
    result = mod.load_or_generate_cookie_secret(tmp_path)

    assert isinstance(result, str), "expected a string secret"
    assert len(result) == 64, f"expected 64 hex chars, got {len(result)}"
    assert all(c in "0123456789abcdef" for c in result), "expected hex string"

    secret_file = tmp_path / "accounts-cookie-secret"
    assert secret_file.exists(), "secret file must be written even without fchmod"
    assert secret_file.read_text().strip() == result


def test_no_crash_when_fchmod_unavailable_uses_fallback_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When os.fchmod is absent, the secret file is created with safe permissions.

    The file must be readable/writable by owner (mode 0o600 from os.open's
    creation flags) — on POSIX os.open(..., 0o600) sets those bits; when
    fchmod is skipped the creation-time mode should still be correct.
    This test verifies the file exists and is non-empty — the mode-assertion
    is POSIX-only and skipped on Windows (where fchmod doesn't exist anyway).
    """
    import sys

    monkeypatch.delenv("OMNIGENT_ACCOUNTS_COOKIE_SECRET", raising=False)

    if hasattr(os, "fchmod"):
        monkeypatch.delattr(os, "fchmod")

    mod = _reload_module()
    result = mod.load_or_generate_cookie_secret(tmp_path)

    secret_file = tmp_path / "accounts-cookie-secret"
    assert secret_file.exists()
    content = secret_file.read_text().strip()
    assert content == result

    if sys.platform != "win32":
        # Creation mode 0o600 was set via os.open; verify no extra bits leaked.
        file_mode = stat.S_IMODE(secret_file.stat().st_mode)
        assert file_mode == 0o600, f"secret file mode should be 0o600, got 0o{file_mode:o}"
