"""A codex (SDK) session signed in with ChatGPT retries a turn throttled before any output.

ChatGPT login routes Codex through its built-in ``openai`` provider, which gives up
on a 429 after one request; here that provider points at the mock model server.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.spec.types import RetryPolicy
from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bind_session_runner, post_session_bundle
from tests.e2e.conftest import isolated_mock_llm_server_url as isolated_mock_llm_server_url
from tests.e2e_ui.conftest import configure_mock_llm
from tests.e2e_ui.sessions.test_codex_multistep_turn_usage import (
    _build_codex_bundle,
    _model_requests,
)

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None, reason="requires the real codex CLI app-server"
)

_REPO = Path(__file__).resolve().parents[3]
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_RATE_LIMITED = {
    "error": "Rate limit reached for requests per min (RPM). Please try again in 20s.",
    "status_code": 429,
}
_RATE_LIMITED_ATTEMPTS = 2
_REPLY = "All systems are running normally."


def _unsigned_jwt(claims: dict, signature: str) -> str:
    def encode(part: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=")

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(claims)}.{signature}"


def _write_chatgpt_codex_home(codex_home: Path, mock_url: str) -> None:
    codex_home.mkdir(parents=True)
    plan = {
        "chatgpt_plan_type": "plus",
        "chatgpt_account_id": "acct-e2e-fake",
        "chatgpt_user_id": "user-e2e-fake",
        "user_id": "user-e2e-fake",
    }
    claims = {"exp": int(time.time()) + 30 * 24 * 3600, "https://api.openai.com/auth": plan}
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": _unsigned_jwt({**claims, "email": "e2e@example.invalid"}, "sig"),
                    "access_token": _unsigned_jwt(claims, "fake-chatgpt-access"),
                    "refresh_token": "fake-chatgpt-refresh",
                    "account_id": "acct-e2e-fake",
                },
                "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
    )
    # Codex ignores OPENAI_BASE_URL; this key redirects only its built-in provider.
    (codex_home / "config.toml").write_text(f'openai_base_url = "{mock_url}/v1"\n')


@contextmanager
def _chatgpt_codex_stack(root: Path, mock_url: str) -> Iterator[ServerRunner]:
    """Run a server and runner whose Codex is signed in with ChatGPT, not an Omnigent provider."""
    config = root / "omnigent-config"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text(json.dumps({"runner": {"idle_timeout_s": 0}}))
    kept = {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    base_env = {key: value for key, value in os.environ.items() if key in kept}
    env = {
        "OMNIGENT_CONFIG_HOME": str(config),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }
    with server_runner(
        root / "stack", server_cwd=_REPO, base_env=base_env, server_env=env, poll_interval=0.2
    ) as stack:
        _write_chatgpt_codex_home(stack.runner_home / ".codex", mock_url)
        stack.start_runner(cwd=_REPO, env=env)
        yield stack


def _open_codex_session(request: pytest.FixtureRequest, stack: ServerRunner, token: str) -> Page:
    bundle = _build_codex_bundle(f"codex-chatgpt-{token}", f"mock-codex-{token}")
    created = post_session_bundle(
        httpx.post,
        f"{stack.base_url}/v1/sessions",
        bundle,
        metadata={"title": "Codex status check"},
        timeout=30.0,
    )
    created.raise_for_status()
    session_id = created.json()["session_id"]
    bind_session_runner(httpx.patch, stack.base_url, session_id, stack.runner_id, timeout=10.0)
    page: Page = request.getfixturevalue("page")
    page.goto(f"{stack.base_url}/c/{session_id}")
    return page


@pytest.mark.timeout(300)
def test_chatgpt_codex_session_retries_a_transient_429_until_the_reply(
    request: pytest.FixtureRequest,
    built_spa: None,
    isolated_mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    mock_url = isolated_mock_llm_server_url
    token = f"throttle-{uuid.uuid4().hex[:8]}"
    responses = [_RATE_LIMITED] * _RATE_LIMITED_ATTEMPTS + [{"text": _REPLY}]
    configure_mock_llm(mock_url, responses, match=token)
    with _chatgpt_codex_stack(tmp_path, mock_url) as stack:
        page = _open_codex_session(request, stack, token)
        composer = page.get_by_role("textbox", name="Message the agent")
        expect(composer).to_be_editable(timeout=30_000)
        composer.fill(f"What is the status? {token}")
        page.get_by_role("button", name="Send", exact=True).click()

        max_attempts = RetryPolicy().max_retries + 1
        expect(page.get_by_text(f"Retrying llm · attempt 2/{max_attempts}")).to_be_visible(
            timeout=120_000
        )
        expect(page.locator(_ASSISTANT, has_text=_REPLY)).to_be_visible(timeout=120_000)
        expect(page.get_by_test_id("error-pill")).to_have_count(0)

    assert len(_model_requests(mock_url, token)) == _RATE_LIMITED_ATTEMPTS + 1
