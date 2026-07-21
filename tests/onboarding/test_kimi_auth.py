from pathlib import Path

from omnigent.onboarding.kimi_auth import kimi_credential_dirs, kimi_login_detected


def test_detects_current_kimi_code_credentials(tmp_path: Path) -> None:
    credentials = tmp_path / ".kimi-code" / "credentials"
    credentials.mkdir(parents=True)
    (credentials / "auth.json").write_text("{}", encoding="utf-8")
    assert kimi_login_detected(tmp_path) is True


def test_detects_legacy_kimi_credentials(tmp_path: Path) -> None:
    credentials = tmp_path / ".kimi" / "credentials"
    credentials.mkdir(parents=True)
    (credentials / "token").write_text("token", encoding="utf-8")
    assert kimi_login_detected(tmp_path) is True


def test_empty_or_missing_kimi_credentials_are_not_a_login(tmp_path: Path) -> None:
    (tmp_path / ".kimi-code" / "credentials").mkdir(parents=True)
    assert kimi_login_detected(tmp_path) is False


def test_expands_user_in_configured_kimi_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("KIMI_CODE_HOME", "~/.kimi-code")

    assert kimi_credential_dirs()[0] == tmp_path / ".kimi-code" / "credentials"
