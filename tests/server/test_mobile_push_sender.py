import asyncio
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock
from urllib.parse import parse_qs

import httpx
import jwt
import pytest

from omnigent.server.feature_flags import resolve_feature_flags
from tests.server.test_mobile_push_config import (
    assert_credentials_redacted,
    verification_key,
)
from tests.server.test_mobile_push_config import credentials as credentials
from tests.server.test_mobile_push_store import push_store as push_store
from tests.server.test_mobile_push_store import register
from tests.server.test_mobile_push_store import session_id as session_id


@pytest.fixture
def deliver(push_store, session_id):
    from omnigent.db.db_models import SqlMobilePushDevice, SqlMobilePushOutbox
    from omnigent.server.mobile_push import MobilePushService
    from omnigent.server.mobile_push_sender import SendResult

    async def run(sender, *, token):
        register(push_store, token=token)
        push_store.enqueue(session_id, "completed", now=100)
        delivery = push_store.claim(now=110)[0]
        acknowledge = Mock(wraps=push_store.acknowledge)
        original = push_store.acknowledge
        push_store.acknowledge = acknowledge
        try:
            service = MobilePushService(push_store, Mock(), sender, preview=False)
            await service._deliver(delivery, now=110)
        finally:
            push_store.acknowledge = original
        assert acknowledge.call_count == 1
        result = SendResult(
            acknowledge.call_args.args[1], acknowledge.call_args.kwargs["retry_after"]
        )
        with push_store._session("verify_production_delivery_result") as transaction:
            row = transaction.get(SqlMobilePushOutbox, (0, delivery.id))
            assert row is not None
            assert row.delivered == (result.outcome != "retry")
            device = transaction.get(SqlMobilePushDevice, (0, "phone"))
            assert (device is None) == (result.outcome == "prune")
        return result

    return run


async def test_oauth_claims_locked_cache_and_early_refresh(credentials, monkeypatch):
    from omnigent.server import mobile_push_sender
    from omnigent.server.mobile_push_config import FCM_SCOPE, TOKEN_ENDPOINT, FcmConfig

    path, document = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None
    assertions = []
    clock = [100.0]
    monkeypatch.setattr(mobile_push_sender, "monotonic", lambda: clock[0])

    def google(request):
        assert str(request.url) == TOKEN_ENDPOINT
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
        assertions.append(form["assertion"][0])
        return httpx.Response(
            200, json={"access_token": "secret-access-token", "expires_in": 3600}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(google), follow_redirects=False
    ) as client:
        sender = mobile_push_sender.FcmSender(config, client)
        tokens = await asyncio.gather(*(sender.access_token() for _ in range(8)))
        assert tokens == ["secret-access-token"] * 8
        assert len(assertions) == 1
        claims = jwt.decode(
            assertions[0],
            key=verification_key(document),
            algorithms=["RS256"],
            audience=TOKEN_ENDPOINT,
        )
        assert claims["iss"] == document["client_email"]
        assert claims["scope"] == FCM_SCOPE
        assert claims["exp"] - claims["iat"] == 3600
        clock[0] = 3641
        assert await sender.access_token() == "secret-access-token"
        assert len(assertions) == 2
        assert "secret-access-token" not in repr(sender)


@pytest.mark.parametrize(
    "status,details,outcome",
    [
        (200, [], "sent"),
        (
            404,
            [
                {
                    "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                    "errorCode": "UNREGISTERED",
                }
            ],
            "prune",
        ),
        (
            403,
            [
                {
                    "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                    "errorCode": "SENDER_ID_MISMATCH",
                }
            ],
            "prune",
        ),
        (
            400,
            [
                {
                    "@type": "type.googleapis.com/google.rpc.BadRequest",
                    "fieldViolations": [{"field": "message.token"}],
                }
            ],
            "prune",
        ),
        (
            400,
            [
                {
                    "@type": "type.googleapis.com/google.rpc.BadRequest",
                    "fieldViolations": [{"field": "message.data"}],
                }
            ],
            "discard",
        ),
        (400, [], "discard"),
        (401, [], "discard"),
        (403, [], "discard"),
        (429, [], "retry"),
        (500, [], "retry"),
        (503, [], "retry"),
    ],
)
async def test_send_classification_and_redaction(
    status, details, outcome, credentials, caplog, deliver
):
    from omnigent.server.mobile_push_config import FcmConfig
    from omnigent.server.mobile_push_sender import FcmSender

    path, document = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None

    def google(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "secret-access-token", "expires_in": 3600}
            )
        assert str(request.url) == config.send_endpoint
        assert request.headers["authorization"] == "Bearer secret-access-token"
        assert json.loads(request.content)["message"]["token"] == "secret-device-token"
        return httpx.Response(
            status,
            headers={"Retry-After": "17"},
            json={"error": {"message": "secret-device-token", "details": details}},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(google), follow_redirects=False
    ) as client:
        sender = FcmSender(config, client)
        result = await deliver(sender, token="secret-device-token")
    assert result.outcome == outcome
    if outcome == "retry":
        assert result.retry_after == 17
    captured = caplog.text + repr(sender) + repr(result)
    assert_credentials_redacted(document, captured)
    for secret in ("secret-device-token", "secret-access-token"):
        assert secret not in captured
    assert not hasattr(FcmSender, "send")


@pytest.mark.parametrize("endpoint", ["oauth", "fcm"])
async def test_google_redirects_are_never_followed(endpoint, credentials, deliver):
    from omnigent.server.mobile_push_config import FcmConfig
    from omnigent.server.mobile_push_sender import FcmSender

    path, _ = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None
    hosts = []

    def google(request):
        hosts.append(request.url.host)
        if endpoint == "fcm" and request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "secret", "expires_in": 3600})
        return httpx.Response(307, headers={"Location": "https://untrusted.invalid/steal"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        result = await deliver(FcmSender(config, client), token="secret")
    assert result.outcome == "discard"
    assert "untrusted.invalid" not in hosts


async def test_transport_failure_retries_without_logging_payload(credentials, caplog, deliver):
    from omnigent.server.mobile_push_config import FcmConfig
    from omnigent.server.mobile_push_sender import FcmSender

    path, _ = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None

    def google(request):
        raise httpx.ConnectError("secret-token", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        result = await deliver(FcmSender(config, client), token="secret-token")
    assert result.outcome == "retry"
    assert "secret-token" not in caplog.text


async def test_token_specific_fcm_invalid_argument_is_pruned(credentials, deliver):
    from omnigent.server.mobile_push_config import FcmConfig
    from omnigent.server.mobile_push_sender import FcmSender

    path, _ = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None

    def google(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "The registration token is not a valid FCM registration token",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                            "errorCode": "INVALID_ARGUMENT",
                        }
                    ],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        assert (await deliver(FcmSender(config, client), token="token")).outcome == "prune"


@pytest.mark.parametrize("endpoint", ["oauth", "fcm"])
async def test_nontransport_http_errors_retry(credentials, endpoint, deliver):
    from omnigent.server.mobile_push_config import FcmConfig
    from omnigent.server.mobile_push_sender import FcmSender

    path, _ = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None

    def google(request):
        if endpoint == "fcm" and request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        raise httpx.DecodingError("sensitive-payload", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        assert (await deliver(FcmSender(config, client), token="token")).outcome == "retry"


async def test_fcm_401_refreshes_once_and_reuses_sender(credentials, deliver):
    from omnigent.server.mobile_push_config import FcmConfig
    from omnigent.server.mobile_push_sender import FcmSender

    path, _ = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None
    oauth = []
    sends = []

    def google(request):
        if request.url.host == "oauth2.googleapis.com":
            oauth.append(request)
            return httpx.Response(
                200, json={"access_token": f"access-{len(oauth)}", "expires_in": 3600}
            )
        sends.append(request.headers["authorization"])
        return httpx.Response(401 if len(sends) == 1 else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        sender = FcmSender(config, client)
        assert (await deliver(sender, token="token")).outcome == "sent"
        assert (await deliver(sender, token="token")).outcome == "sent"
    assert len(oauth) == 2
    assert sends == ["Bearer access-1", "Bearer access-2", "Bearer access-2"]


@pytest.mark.parametrize(
    "endpoint,status", [("oauth", 400), ("oauth", 401), ("oauth", 403), ("fcm", 403)]
)
async def test_auth_failures_warn_status_only_and_back_off(
    credentials, caplog, endpoint, status, deliver
):
    from omnigent.server.mobile_push_config import FcmConfig
    from omnigent.server.mobile_push_sender import FcmSender

    path, _ = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None
    calls = []

    def google(request):
        calls.append(request)
        if endpoint == "fcm" and request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "sensitive-access", "expires_in": 3600}
            )
        return httpx.Response(
            status, json={"error": {"status": "PERMISSION_DENIED", "message": "sensitive-body"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        sender = FcmSender(config, client)
        first = await deliver(sender, token="sensitive-device")
        second = await deliver(sender, token="sensitive-device")
    assert first.outcome == second.outcome == "retry"
    assert first.retry_after >= 30
    assert len(calls) == (2 if endpoint == "fcm" else 1)
    warnings = [
        record
        for record in caplog.records
        if record.name.endswith("mobile_push_sender") and record.levelname == "WARNING"
    ]
    assert len(warnings) == 1
    assert str(status) in warnings[0].message
    assert "sensitive" not in caplog.text


@pytest.mark.parametrize(
    "header_form,requested_delay,expected_delay",
    [
        pytest.param("delta_seconds", 300, 300, id="numeric"),
        pytest.param("http_date", 300, 300, id="http-date"),
        pytest.param("delta_seconds", 7200, 3600, id="numeric-clamped"),
        pytest.param("http_date", 7200, 3600, id="http-date-clamped"),
        pytest.param("delta_seconds", 17, 60, id="numeric-minimum"),
        pytest.param("http_date", 17, 60, id="http-date-minimum"),
        pytest.param("http_date", -300, 60, id="past-date-default"),
        pytest.param("invalid", 0, 60, id="invalid-date-default"),
    ],
)
async def test_permission_denied_retry_after_controls_outbox_and_shared_cooldown(
    credentials, push_store, session_id, monkeypatch, header_form, requested_delay, expected_delay
):
    from omnigent.db.db_models import SqlMobilePushOutbox
    from omnigent.server import mobile_push_sender
    from omnigent.server.mobile_push import MobilePushService
    from omnigent.server.mobile_push_config import FcmConfig

    path, _ = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None
    clock = [110.0]
    wall_clock = datetime(2026, 10, 2, 12, 0, 0, 250000, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return wall_clock

    monkeypatch.setattr(mobile_push_sender, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mobile_push_sender, "datetime", FrozenDateTime)
    retry_after = (
        str(requested_delay)
        if header_form == "delta_seconds"
        else format_datetime(wall_clock + timedelta(seconds=requested_delay), usegmt=True)
    )
    if header_form == "invalid":
        retry_after = "not a valid HTTP date"
    calls = []

    def google(request):
        calls.append(request.url.host)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        if calls.count("fcm.googleapis.com") == 1:
            return httpx.Response(
                403,
                headers={"Retry-After": retry_after},
                json={"error": {"status": "PERMISSION_DENIED"}},
            )
        return httpx.Response(200)

    register(push_store)
    push_store.enqueue(session_id, "completed", now=100)
    first = push_store.claim(now=110)[0]
    deadline = 110 + expected_delay

    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        sender = mobile_push_sender.FcmSender(config, client)
        service = MobilePushService(push_store, Mock(), sender, preview=False)
        await service._deliver(first, now=110)
        with push_store._session("verify_permission_denied_retry_deadline") as transaction:
            row = transaction.get(SqlMobilePushOutbox, (0, first.id))
            assert row is not None and not row.delivered
            assert row.not_before == deadline
        assert calls == ["oauth2.googleapis.com", "fcm.googleapis.com"]

        clock[0] = 140
        register(push_store, user="reader", installation="reader-phone", token="reader-token")
        push_store.enqueue(session_id, "completed", now=130)
        second = push_store.claim(now=140)[0]
        assert second.installation_id == "reader-phone"
        await service._deliver(second, now=140)
        with push_store._session("verify_shared_authorization_retry_deadline") as transaction:
            row = transaction.get(SqlMobilePushOutbox, (0, second.id))
            assert row is not None and not row.delivered
            assert row.not_before == deadline
        assert push_store.claim(now=deadline - 1) == []
        for stamp in (140, deadline - 1, deadline - 0.25):
            clock[0] = stamp
            result = await sender.authorization()
            assert isinstance(result, mobile_push_sender.SendResult)
            assert result.outcome == "retry"
            assert calls == ["oauth2.googleapis.com", "fcm.googleapis.com"]

        clock[0] = deadline
        token = await sender.authorization()
        assert isinstance(token, str)
        assert (await sender.post({"message": {"token": "device-token"}}, token)).outcome == "sent"
        assert calls == [
            "oauth2.googleapis.com",
            "fcm.googleapis.com",
            "oauth2.googleapis.com",
            "fcm.googleapis.com",
        ]


@pytest.mark.parametrize("third_party", [True, False])
async def test_fcm_third_party_and_persistent_401_warn_without_pruning(
    credentials, caplog, deliver, third_party
):
    from omnigent.server.mobile_push_config import FcmConfig
    from omnigent.server.mobile_push_sender import FcmSender

    path, document = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None
    calls = []

    def google(request):
        calls.append(request.url.host)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "sensitive-access", "expires_in": 3600}
            )
        return httpx.Response(
            401,
            json={
                "error": {
                    "message": "sensitive-body",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                            "errorCode": "THIRD_PARTY_AUTH_ERROR"
                            if third_party
                            else "UNAUTHENTICATED",
                        }
                    ],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        sender = FcmSender(config, client)
        assert (await deliver(sender, token="sensitive-device")).outcome == "discard"
        assert calls.count("oauth2.googleapis.com") == (1 if third_party else 2)
        assert calls.count("fcm.googleapis.com") == (1 if third_party else 2)
        assert (await deliver(sender, token="sensitive-device")).outcome == "discard"
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "status=401" in warnings[0].message
    assert ("THIRD_PARTY_AUTH_ERROR" in warnings[0].message) == third_party
    assert "sensitive" not in caplog.text
    assert_credentials_redacted(document, caplog.text)


@pytest.mark.parametrize("oauth_status", [401, 403])
async def test_warning_limits_are_independent_per_failure_class(
    credentials, caplog, deliver, monkeypatch, oauth_status
):
    from omnigent.server import mobile_push_sender
    from omnigent.server.mobile_push_config import FcmConfig

    path, document = credentials
    config = FcmConfig.from_env(
        resolve_feature_flags({"OMNIGENT_FEATURES": "mobile_push"}),
        {"OMNIGENT_FCM_CREDENTIALS_FILE": str(path)},
    )
    assert config is not None
    clock = [100.0]
    monkeypatch.setattr(mobile_push_sender, "monotonic", lambda: clock[0])
    oauth_calls = []

    def google(request):
        if request.url.host == "oauth2.googleapis.com":
            oauth_calls.append(request)
            if len(oauth_calls) == 1:
                return httpx.Response(
                    200, json={"access_token": "sensitive-access", "expires_in": 60}
                )
            return httpx.Response(oauth_status, json={"error": "sensitive-provider-error"})
        return httpx.Response(
            401,
            json={
                "error": {
                    "message": "sensitive-body",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                            "errorCode": "THIRD_PARTY_AUTH_ERROR",
                        }
                    ],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(google)) as client:
        sender = mobile_push_sender.FcmSender(config, client)
        assert (await deliver(sender, token="sensitive-device")).outcome == "discard"
        assert (await deliver(sender, token="sensitive-device")).outcome == "retry"
        assert (await deliver(sender, token="sensitive-device")).outcome == "retry"
        warnings = [record for record in caplog.records if record.levelname == "WARNING"]
        assert len(warnings) == 2
        assert "THIRD_PARTY_AUTH_ERROR" in warnings[0].message
        assert f"status={oauth_status}" in warnings[1].message
        assert "THIRD_PARTY_AUTH_ERROR" not in warnings[1].message
        for elapsed in (0, 59):
            clock[0] = 100 + elapsed
            sender.warn_auth_failure(401, third_party=True)
            sender.warn_auth_failure(oauth_status)
        assert len([record for record in caplog.records if record.levelname == "WARNING"]) == 2
        clock[0] = 160
        sender.warn_auth_failure(401, third_party=True)
        sender.warn_auth_failure(oauth_status)
        assert len([record for record in caplog.records if record.levelname == "WARNING"]) == 4
    assert len(oauth_calls) == 2
    assert "sensitive" not in caplog.text
    assert_credentials_redacted(document, caplog.text)
