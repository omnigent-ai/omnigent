import json
import logging
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from omnigent.server.feature_flags import resolve_feature_flags


def verification_key(document: dict[str, str]) -> rsa.RSAPublicKey:
    key = serialization.load_pem_private_key(document["private_key"].encode(), password=None)
    assert isinstance(key, rsa.RSAPrivateKey)
    return key.public_key()


def assert_credentials_redacted(document: dict[str, str], captured: str) -> None:
    assert document["private_key"] not in captured
    assert "PRIVATE KEY" not in captured


@pytest.fixture
def credentials(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    document = {
        "type": "service_account",
        "project_id": "push-project",
        "client_email": "push@push-project.iam.gserviceaccount.com",
        "private_key": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "token_uri": "https://untrusted.invalid/token",
    }
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(document))
    return path, document


@pytest.mark.parametrize(
    "flag,path_present", [(False, False), (False, True), (True, False), (True, True)]
)
def test_activation_states(flag, path_present, credentials, caplog):
    from omnigent.server.mobile_push_config import FcmConfig

    path, document = credentials
    environ = {"OMNIGENT_FEATURES": "mobile_push" if flag else ""}
    if path_present:
        environ["OMNIGENT_FCM_CREDENTIALS_FILE"] = str(path)
    with caplog.at_level(logging.INFO):
        config = FcmConfig.from_env(resolve_feature_flags(environ), environ)
    assert (config is not None) == (flag and path_present)
    assert len(caplog.records) == int(flag != path_present)
    if flag and not path_present:
        assert caplog.records[0].levelno == logging.WARNING
    if path_present and not flag:
        assert caplog.records[0].levelno == logging.INFO
    if config:
        assert config.project_id == document["project_id"]
        assert document["private_key"] not in repr(config)
        assert "PRIVATE KEY" not in repr(config)


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "authorized_user"),
        ("project_id", "../escape"),
        ("client_email", ""),
        ("private_key", "not-a-key"),
    ],
)
def test_invalid_credentials_fail_safely(field, value, credentials, caplog):
    from omnigent.server.mobile_push_config import FcmConfig

    path, document = credentials
    document[field] = value
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="FCM") as error:
        FcmConfig.from_env(
            resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
            {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
        )
    assert "not-a-key" not in str(error.value)
    assert "PRIVATE KEY" not in str(error.value)
    assert not caplog.records


def test_ambient_credentials_and_disabled_invalid_file_are_ignored(tmp_path):
    from omnigent.server.mobile_push_config import FcmConfig

    flags = resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"})
    assert (
        FcmConfig.from_env(flags, {"GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "missing")})
        is None
    )
    assert (
        FcmConfig.from_env(
            resolve_feature_flags({}), {"OMNIGENT_FCM_CREDENTIALS_FILE": str(tmp_path / "missing")}
        )
        is None
    )
    with pytest.raises(ValueError, match="FCM"):
        FcmConfig.from_env(flags, {"OMNIGENT_FCM_CREDENTIALS_FILE": str(tmp_path / "missing")})


def test_preview_is_a_non_secret_setting(monkeypatch):
    from omnigent.server.server_config import mobile_push_preview

    monkeypatch.delenv("OMNIGENT_MOBILE_PUSH_PREVIEW", raising=False)
    assert not mobile_push_preview({})
    assert mobile_push_preview({"mobile_push_preview": True})
    monkeypatch.setenv("OMNIGENT_MOBILE_PUSH_PREVIEW", "0")
    assert not mobile_push_preview({"mobile_push_preview": True})


def test_golden_formatter_and_platform_payloads():
    from omnigent.server.mobile_push_content import format_notification, message_payload

    cases = json.loads(
        (Path(__file__).parents[1] / "fixtures/mobile_push_content.json").read_text()
    )
    for case in cases:
        inputs = case["input"]
        assert format_notification(**inputs) == tuple(case["output"])
        for platform in ("android", "ios"):
            payload = message_payload(
                platform=platform, token="sensitive-token", session_id="session", **inputs
            )
            data = payload["message"]["data"]
            assert data["session_id"] == "session"
            assert data["title"] == case["output"][0]
            if platform == "android":
                assert "notification" not in payload["message"]
                assert payload["message"]["android"] == {
                    "priority": "high",
                    "ttl": "3600s",
                    "collapse_key": "session",
                }
            else:
                alert = payload["message"]["apns"]["payload"]["aps"]["alert"]
                assert alert == dict(zip(("title", "body"), case["output"], strict=True))
                assert payload["message"]["apns"]["headers"]["apns-collapse-id"] == "session"


def test_only_reviewed_error_codes_are_rendered():
    from omnigent.server.mobile_push_content import failure_reason, format_notification

    assert failure_reason("runner_unavailable") == "Runner unavailable"
    assert failure_reason("provider-body-with-secret") is None
    assert format_notification("failed", "My session", reason="Runner unavailable") == (
        "My session",
        "Agent stopped with an error: Runner unavailable",
    )
    assert format_notification("failed", "My session", reason="provider-body-with-secret") == (
        "My session",
        "Agent stopped with an error.",
    )
    assert format_notification("failed", "My session:closed:conv_old", reason=None) == (
        "My session",
        "Agent stopped with an error.",
    )
