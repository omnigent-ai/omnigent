"""Account probes expose identity metadata without credential material."""

from __future__ import annotations

import json
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from omnigent import harness_accounts as account_types
from omnigent.inner import _proc
from omnigent.onboarding import harness_accounts, harness_login
from omnigent.onboarding.opencode_auth import OpenCodeAuthSummary
from tests._helpers.polling import poll_until


def _executable(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_codex_probe_reads_chatgpt_identity_from_app_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "codex",
        """import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get('method') == 'initialize':
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
    elif message.get('method') == 'account/read':
        print(json.dumps({'id': message['id'], 'result': {'account': {
            'type': 'chatgpt', 'email': 'person@example.test', 'planType': 'pro'
        }}}), flush=True)
""",
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    assert harness_accounts.harness_account_map() == {
        "codex": {
            "kind": "chatgpt",
            "email": "person@example.test",
            "plan": "pro",
        }
    }


@pytest.mark.asyncio
async def test_codex_login_and_logout_use_app_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "codex",
        """import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if method == 'initialize':
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
    elif method == 'account/login/start':
        print(json.dumps({'id': message['id'], 'result': {
            'type': 'chatgptDeviceCode', 'loginId': 'login-1',
            'userCode': 'ABCD-EFGH',
            'verificationUrl': 'https://example.test/device'
        }}), flush=True)
        print(json.dumps({'method': 'account/login/completed', 'params': {
            'loginId': 'login-1', 'success': True
        }}), flush=True)
    elif method == 'account/logout':
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
""",
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    session = harness_login.CodexLoginSession()
    try:
        login = await session.start()
        assert login == harness_login.CodexDeviceLogin(
            login_id="login-1",
            verification_url="https://example.test/device",
            user_code="ABCD-EFGH",
        )
        assert await session.wait_completed(login.login_id) == (True, None)
    finally:
        await session.close()

    await harness_login.codex_logout()


@pytest.mark.asyncio
async def test_codex_login_rejects_non_https_verification_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "codex",
        """import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get('method') == 'initialize':
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
    elif message.get('method') == 'account/login/start':
        print(json.dumps({'id': message['id'], 'result': {
            'loginId': 'login-1', 'userCode': 'ABCD-EFGH',
            'verificationUrl': 'javascript:alert(1)'
        }}), flush=True)
""",
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    session = harness_login.CodexLoginSession()
    try:
        with pytest.raises(RuntimeError, match="invalid device login URL"):
            await session.start()
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_claude_logout_uses_supported_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "claude",
        """import sys
raise SystemExit(0 if sys.argv[1:] == ['auth', 'logout'] else 2)
""",
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    await harness_login.claude_logout()


def test_claude_and_pi_probes_use_supported_json_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "claude",
        """import json
print(json.dumps({'loggedIn': True, 'authMethod': 'claudeai',
 'email': 'claude@example.test', 'organization': 'Example',
 'subscriptionType': 'max'}))
""",
    )
    _executable(
        bin_dir / "pi",
        """import json, sys
provider = sys.argv[sys.argv.index('--provider') + 1]
if provider == 'anthropic':
    print(json.dumps({'status': 'ready', 'provider': provider, 'authType': 'oauth'}))
else:
    print(json.dumps({'status': 'not_ready', 'provider': provider,
                      'reason': 'credentials_not_configured'}))
    raise SystemExit(1)
""",
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    accounts = harness_accounts.harness_account_map()
    assert accounts["claude"] == {
        "kind": "subscription",
        "email": "claude@example.test",
        "org": "Example",
        "plan": "max",
    }
    assert accounts["pi"] == {
        "kind": "providers",
        "providers": [{"id": "anthropic", "label": "Anthropic", "auth_type": "oauth"}],
    }


def test_pi_probe_shares_one_deadline_and_keeps_partial_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadlines: list[float] = []

    def _run(
        argv: list[str],
        *,
        deadline: float,
        cancel_event: threading.Event | None,
    ) -> tuple[int, dict[str, object] | None]:
        del cancel_event
        deadlines.append(deadline)
        provider = argv[argv.index("--provider") + 1]
        if provider == "anthropic":
            return 0, {"status": "ready", "authType": "oauth"}
        return 1, None

    monkeypatch.setattr(harness_accounts.shutil, "which", lambda command: command)
    monkeypatch.setattr(harness_accounts, "_run_json_command", _run)

    account = harness_accounts.probe_harness_account("pi", timeout_s=7)

    assert account == {
        "kind": "providers",
        "providers": [{"id": "anthropic", "label": "Anthropic", "auth_type": "oauth"}],
    }
    assert len(deadlines) == 3
    assert len(set(deadlines)) == 1


def test_json_account_probes_do_not_inherit_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Process:
        pid = None
        returncode = 0

        def communicate(self, *, timeout: float) -> tuple[str, str]:
            del timeout
            return "{}", ""

    def _popen(argv: list[str], **kwargs: object) -> _Process:
        assert argv == ["probe", "--json"]
        captured.update(kwargs)
        return _Process()

    monkeypatch.setattr(harness_accounts.subprocess, "Popen", _popen)

    return_code, payload = harness_accounts._run_json_command(
        ["probe", "--json"],
        deadline=time.monotonic() + 1,
        cancel_event=None,
    )

    assert return_code == 0
    assert payload == {}
    assert captured["stdin"] is harness_accounts.subprocess.DEVNULL


def test_cancelled_claude_probe_kills_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled account probe cannot leave a pipe-holding CLI child behind."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    child_pid_path = tmp_path / "child.pid"
    _executable(
        bin_dir / "claude",
        """import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
with open(os.environ['CHILD_PID_PATH'], 'w') as stream:
    stream.write(str(child.pid))
time.sleep(60)
""",
    )
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("CHILD_PID_PATH", str(child_pid_path))
    cancel_event = threading.Event()
    result: list[object] = []
    worker = threading.Thread(
        target=lambda: result.append(
            harness_accounts.probe_harness_account(
                "claude", timeout_s=10, cancel_event=cancel_event
            )
        )
    )
    worker.start()
    deadline = time.monotonic() + 2
    while not child_pid_path.exists() and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert child_pid_path.exists()
    child_pid = int(child_pid_path.read_text())

    cancel_event.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == [{"kind": "unknown"}]
    poll_until(lambda: not _proc.process_alive(child_pid), timeout_s=2, interval_s=0.01)


def test_opencode_probe_returns_only_non_secret_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "opencode", "raise SystemExit(0)\n")
    data_home = tmp_path / "data"
    auth_path = data_home / "opencode" / "auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(
        json.dumps(
            {
                "anthropic": {"type": "api", "key": "must-not-leak"},
                "github": {
                    "type": "oauth",
                    "access": "must-not-leak",
                    "refresh": "must-not-leak",
                    "expires": 1,
                },
            }
        )
    )
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    for _provider_id, _label, env_var in harness_accounts._ENV_PROVIDER_VARS:
        monkeypatch.delenv(env_var, raising=False)

    account = harness_accounts.harness_account_map()["opencode"]
    assert account == {
        "kind": "providers",
        "providers": [
            {"id": "anthropic", "label": "anthropic", "auth_type": "stored"},
            {"id": "github", "label": "github", "auth_type": "stored"},
        ],
    }
    assert "must-not-leak" not in json.dumps(account)


def test_normalizer_drops_secret_and_malformed_fields() -> None:
    normalized = account_types.normalize_harness_accounts(
        {
            "codex": {
                "kind": "chatgpt",
                "email": "person@example.test",
                "token": "secret",
            },
            "bad": {"kind": "future"},
        }
    )

    assert normalized == {"codex": {"kind": "chatgpt", "email": "person@example.test"}}


def test_codex_probe_skips_notifications_buffered_before_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "codex",
        """import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get('method') == 'initialize':
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
    elif message.get('method') == 'account/read':
        payload = (
            json.dumps({'method': 'account/updated', 'params': {}}) + '\\n' +
            json.dumps({'id': message['id'], 'result': {'account': {
                'type': 'chatgpt', 'email': 'buffered@example.test'
            }}}) + '\\n'
        )
        os.write(sys.stdout.fileno(), payload.encode())
""",
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    assert harness_accounts.harness_account_map()["codex"] == {
        "kind": "chatgpt",
        "email": "buffered@example.test",
    }


def test_codex_refresh_token_is_only_requested_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "refresh-calls.jsonl"
    _executable(
        bin_dir / "codex",
        """import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get('method') == 'initialize':
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
    elif message.get('method') == 'account/read':
        with open(os.environ['REFRESH_CALLS'], 'a') as stream:
            stream.write(json.dumps(message['params']) + '\\n')
        print(json.dumps({'id': message['id'], 'result': {'account': None}}), flush=True)
""",
    )
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("REFRESH_CALLS", str(calls))

    background = harness_accounts.harness_account_map()
    refreshed = harness_accounts.harness_account_map(refresh_codex=True)

    assert [json.loads(line) for line in calls.read_text().splitlines()] == [
        {"refreshToken": False},
        {"refreshToken": True},
    ]
    assert background["codex"] == {"kind": "none"}
    assert refreshed["codex"] == {"kind": "none"}


def test_inconclusive_and_logged_out_probes_are_not_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "codex", "raise SystemExit(1)\n")
    _executable(
        bin_dir / "claude",
        """import json
print(json.dumps({'loggedIn': False}))
raise SystemExit(1)
""",
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    accounts = harness_accounts.harness_account_map()
    assert accounts["codex"] == {"kind": "unknown"}
    assert accounts["claude"] == {"kind": "none"}


def test_codex_explicit_revocation_is_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "codex",
        """import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get('method') == 'initialize':
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
    elif message.get('method') == 'account/read':
        print(json.dumps({'id': message['id'], 'error': {'code': 'token_revoked'}}), flush=True)
""",
    )
    monkeypatch.setenv("PATH", str(bin_dir))

    assert harness_accounts.harness_account_map()["codex"] == {
        "kind": "unknown",
        "expired": True,
    }


def test_opencode_probe_includes_env_and_config_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "opencode", "raise SystemExit(0)\n")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setattr(
        harness_accounts,
        "opencode_auth_summary",
        lambda: OpenCodeAuthSummary(
            installed=True,
            stored_providers=(),
            env_providers=("OpenAI", "Google Gemini"),
            configured_providers=("amazon-bedrock",),
        ),
    )

    account = harness_accounts.harness_account_map()["opencode"]
    assert account == {
        "kind": "providers",
        "providers": [
            {"id": "openai", "label": "OpenAI", "auth_type": "env"},
            {"id": "google", "label": "Google Gemini", "auth_type": "env"},
            {"id": "amazon-bedrock", "label": "amazon-bedrock", "auth_type": "config"},
        ],
    }
    assert "must-not-leak" not in json.dumps(account)
