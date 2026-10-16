"""Setup-to-launch credential tests using isolated config and fake secrets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.harnesses.antigravity_native.bridge import ensure_agy_feedback_survey_disabled
from omnigent.harnesses.antigravity_native.credentials import (
    antigravity_credentials_ready,
    resolve_antigravity_credentials,
)
from omnigent.harnesses.antigravity_native.launch import build_agy_launch
from omnigent.onboarding import secrets
from omnigent.onboarding.configure_models import build_gateway_provider_entry
from omnigent.onboarding.gemini_gateway import GEMINI_API_BASE_URL, validate_gemini_base_url


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    for key in ("GEMINI_API_KEY", "ANTIGRAVITY_API_KEY", "GOOGLE_GEMINI_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"OMNIGENT_{key}", raising=False)
    monkeypatch.setattr(
        "omnigent.harnesses.antigravity_native.launch.agy_binary_path", lambda: "/test/agy"
    )
    monkeypatch.setattr("omnigent.onboarding.gemini_auth.gemini_login_detected", lambda: False)
    monkeypatch.setattr(
        "omnigent.harnesses.antigravity_native.launch.gemini_auth_has_credential", lambda: False
    )


def save_provider(tmp_path: Path, *, ref: str = "keychain:gateway") -> None:
    entry = build_gateway_provider_entry(
        "https://gateway.example/gemini/",
        ref,
        families=["gemini"],
        models={"gemini": "Gemini 3.1 Pro (High)"},
    )
    entry["default"] = ["gemini"]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"providers": {"gateway": entry}}))


@pytest.mark.parametrize("resume", [False, True])
def test_saved_gateway_drives_launch_and_isolated_settings(tmp_path: Path, resume: bool) -> None:
    secrets.store_secret("gateway", "gateway-fake-key")
    save_provider(tmp_path)
    assert antigravity_credentials_ready()
    argv, env = build_agy_launch(
        conversation_id="existing" if resume else None, model=None, resume=resume
    )
    assert env == {
        "GEMINI_API_KEY": "gateway-fake-key",
        "GOOGLE_GEMINI_BASE_URL": "https://gateway.example/gemini",
    }
    assert argv[argv.index("--model") + 1] == "Gemini 3.1 Pro (High)"
    assert ("--conversation" in argv) == resume
    assert "gateway-fake-key" not in " ".join(argv)
    assert "gateway-fake-key" not in repr(resolve_antigravity_credentials())
    ensure_agy_feedback_survey_disabled(tmp_path / "session", launch_env=env)
    settings = json.loads((tmp_path / "session/.gemini/antigravity-cli/settings.json").read_text())
    assert settings["modelProvider"] == "gemini"
    assert "gateway-fake-key" not in json.dumps(settings)


def test_explicit_model_wins(tmp_path: Path) -> None:
    secrets.store_secret("gateway", "fake-key")
    save_provider(tmp_path)
    argv, _ = build_agy_launch(conversation_id=None, model="Gemini 3.8 Flash (Low)", resume=False)
    assert argv[argv.index("--model") + 1] == "Gemini 3.8 Flash (Low)"


def test_selected_provider_keeps_credential_and_endpoint_together(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secrets.store_secret("gateway", "configured-key")
    save_provider(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-key")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://ambient.example/")
    assert resolve_antigravity_credentials().environment() == {
        "GEMINI_API_KEY": "configured-key",
        "GOOGLE_GEMINI_BASE_URL": "https://gateway.example/gemini",
    }


def test_missing_selected_secret_never_falls_back_to_ambient_or_oauth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    save_provider(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-key")
    monkeypatch.setattr("omnigent.onboarding.gemini_auth.gemini_login_detected", lambda: True)
    assert not antigravity_credentials_ready()
    with pytest.raises(OmnigentError):
        build_agy_launch(conversation_id=None, model=None, resume=False)


@pytest.mark.parametrize(
    "harness", ["antigravity-native", "native-antigravity", "agy-native", "native-agy"]
)
def test_host_setup_hint_preserves_saved_credential_error(tmp_path, harness):
    from omnigent.onboarding.harness_install import harness_setup_hint

    save_provider(tmp_path)
    hint = harness_setup_hint(harness)
    assert "no stored secret" in hint
    assert "Run omni setup on the host" in hint
    assert "install.sh" not in hint


def test_env_reference_resolved_at_launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    save_provider(tmp_path, ref="env:CORPORATE_GEMINI_KEY")
    monkeypatch.setenv("CORPORATE_GEMINI_KEY", "rotated-fake-key")
    assert resolve_antigravity_credentials().api_key == "rotated-fake-key"


def test_legacy_listing_url_is_not_used_for_native_requests(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "gemini": {
                        "kind": "key",
                        "default": True,
                        "gemini": {
                            "base_url": GEMINI_API_BASE_URL + "/v1beta/openai",
                            "api_key": "fake",
                        },
                    }
                }
            }
        )
    )
    assert resolve_antigravity_credentials().base_url == GEMINI_API_BASE_URL


def test_legacy_setup_key_is_usable_by_native_agy(tmp_path: Path) -> None:
    secrets.store_secret("antigravity", "legacy-fake")
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"antigravity": {"api_key_ref": "keychain:antigravity"}})
    )
    credentials = resolve_antigravity_credentials()
    assert credentials.api_key == "legacy-fake"
    assert credentials.base_url == GEMINI_API_BASE_URL


@pytest.mark.parametrize("prefix", ["", "OMNIGENT_"])
def test_ambient_gateway(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    monkeypatch.setenv(prefix + "GEMINI_API_KEY", "ambient-fake")
    monkeypatch.setenv(prefix + "GOOGLE_GEMINI_BASE_URL", "https://ambient.example/gemini/")
    assert resolve_antigravity_credentials().base_url == "https://ambient.example/gemini"


def test_oauth_fallback_removes_bridge_owned_gemini_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("omnigent.onboarding.gemini_auth.gemini_login_detected", lambda: True)
    assert antigravity_credentials_ready()
    assert resolve_antigravity_credentials() is None
    ensure_agy_feedback_survey_disabled(tmp_path, launch_env={"GEMINI_API_KEY": "fake"})
    _, env = build_agy_launch(conversation_id=None, model=None, resume=False)
    assert env == {}
    ensure_agy_feedback_survey_disabled(tmp_path, launch_env=env)
    settings = json.loads((tmp_path / ".gemini/antigravity-cli/settings.json").read_text())
    assert "modelProvider" not in settings


@pytest.mark.parametrize(
    "url",
    [
        "https://gateway.example/v1beta",
        "https://gateway.example/v1beta/openai/",
        "https://gateway.example/openai/",
        "https://gateway.example/v1beta/models/gemini:generateContent",
        "https://user:secret@gateway.example",
        "https://gateway.example/?key=secret",
        "file:///tmp/gateway",
        "https://gateway.example:invalid",
    ],
)
def test_invalid_gateway_url_is_actionable_and_does_not_echo_secrets(url: str) -> None:
    with pytest.raises(OmnigentError) as exc:
        validate_gemini_base_url(url)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://workspace.cloud.databricks.com/",
        "https://workspace.azuredatabricks.net/ai-gateway/gemini",
        "https://workspace.gcp.databricks.com/ai-gateway/mlflow/v1/responses",
        "https://gateway.example/v1/responses",
        "https://gateway.example/v1/chat/completions",
        "https://api.openai.com/v1",
        "https://API.OPENAI.COM./v1",
    ],
)
def test_incompatible_saved_gateway_blocks_launch_without_ambient_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str
) -> None:
    entry = {
        "kind": "gateway",
        "default": ["gemini"],
        "gemini": {"base_url": url, "api_key_ref": "keychain:gateway"},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"providers": {"gateway": entry}}))
    secrets.store_secret("gateway", "configured-key")
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-key")
    with pytest.raises(OmnigentError):
        build_agy_launch(conversation_id=None, model=None, resume=False)
    assert not antigravity_credentials_ready()


@pytest.mark.parametrize("url", ["https://gateway.example/v1", "https://openai.com.example/v1"])
def test_custom_gemini_root_can_use_v1(url: str) -> None:
    assert validate_gemini_base_url(url) == url


@pytest.mark.parametrize("missing_key", [False, True])
def test_agy_cli_reports_invalid_credentials_without_crash_reporting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing_key: bool
) -> None:
    from click.testing import CliRunner

    from omnigent.cli import cli

    save_provider(tmp_path)
    if not missing_key:
        config = yaml.safe_load((tmp_path / "config.yaml").read_text())
        config["providers"]["gateway"]["gemini"]["base_url"] = (
            "https://gateway.example/v1/responses"
        )
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
        secrets.store_secret("gateway", "fake-key")
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda _: "http://127.0.0.1:1")
    monkeypatch.setattr(
        "omnigent.harnesses.antigravity_native.main.agy_binary_path", lambda: "agy"
    )
    monkeypatch.setattr(
        "omnigent.harnesses.antigravity_native.main._preflight_local_tools", lambda: None
    )
    result = CliRunner().invoke(cli, ["agy"])
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert ("setup --no-internal-beta" if missing_key else "OpenAI Responses") in result.output
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, OmnigentError)


@pytest.mark.parametrize("kind", ["gateway", "local"])
@pytest.mark.parametrize("compatible", [True, False])
def test_upgrade_of_existing_gemini_block_preserves_other_defaults(
    monkeypatch, tmp_path, kind, compatible
):
    from omnigent.onboarding.provider_config import default_provider_for_harness

    config = {
        "providers": {
            "existing": {
                "kind": kind,
                "default": True,
                "openai": {"base_url": "https://gateway.example/v1", "api_key": "openai-fake"},
                "gemini": {
                    "base_url": "https://gateway.example/gemini"
                    if compatible
                    else "https://gateway.example/v1/responses",
                    "api_key": "gemini-fake",
                },
            }
        }
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    before = path.read_bytes()
    monkeypatch.setattr("omnigent.onboarding.gemini_auth.gemini_login_detected", lambda: True)
    assert default_provider_for_harness(config, "native-codex").name == "existing"
    if compatible:
        _, env = build_agy_launch(conversation_id=None, model=None, resume=False)
        assert env["GOOGLE_GEMINI_BASE_URL"] == "https://gateway.example/gemini"
    else:
        with pytest.raises(OmnigentError, match="OpenAI Responses"):
            build_agy_launch(conversation_id=None, model=None, resume=False)
    assert path.read_bytes() == before
    # Remove only Gemini's default to recover OAuth without changing Codex routing.
    config["providers"]["existing"]["default"] = ["openai"]
    path.write_text(yaml.safe_dump(config))
    assert build_agy_launch(conversation_id=None, model=None, resume=False)[1] == {}
    assert default_provider_for_harness(config, "native-codex").name == "existing"


@pytest.mark.parametrize(
    "profile_text",
    [
        "token = private-malformed-secret\n",
        "[broken]\nhost = https://workspace.example\nprivate-malformed-secret\n",
        "[broken]\nhost = https://one.example\nhost = https://two.example\n",
    ],
)
def test_cli_and_readiness_handle_malformed_databricks_profile(
    monkeypatch, tmp_path, profile_text
):
    from click.testing import CliRunner

    from omnigent.cli import cli

    profile = tmp_path / "databrickscfg"
    profile.write_text(profile_text)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(profile))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "broken": {
                        "kind": "databricks",
                        "profile": "broken",
                        "native_gemini": True,
                        "default": ["gemini"],
                    }
                }
            }
        )
    )
    monkeypatch.setattr(
        "omnigent.harnesses.antigravity_native.main.agy_binary_path", lambda: "agy"
    )
    monkeypatch.setattr(
        "omnigent.harnesses.antigravity_native.main._preflight_local_tools", lambda: None
    )
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda _: "http://127.0.0.1:1")
    result = CliRunner().invoke(cli, ["agy"])
    assert result.exit_code == 1
    assert "Repair" in result.output
    assert "profile" in result.output
    assert "Traceback" not in result.output
    assert "private-malformed-secret" not in result.output
    assert not isinstance(result.exception, OmnigentError)
    assert not antigravity_credentials_ready()
