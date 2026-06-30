"""Tests for the daemon-backed server resolution in the CLI.

Under the daemon model every ``run`` / ``claude`` invocation
ensures the host daemon and targets either the given ``--server`` URL or
a daemon-started local Omnigent server. Covers ``_ensure_host_daemon`` (local vs
remote spawn + reuse), ``_ensure_backend`` (the single resolver), and
``_discover_local_server_url`` (the CLI-side handshake), plus the command
wiring that routes ``--server`` through them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner
from rich.console import Console

# Import the daemon's module chain eagerly: ``_ensure_host_daemon`` imports
# ``omnigent.host.connect`` lazily, and the daemon-spawn tests below patch
# the process-wide ``subprocess.Popen``. Running that import for the first
# time *while* Popen is patched would evaluate ``subprocess.Popen[...]``
# generic aliases in the import chain against the stub (not subscriptable).
import omnigent.host.connect  # noqa: F401
from omnigent import cli
from omnigent.cli import (
    _build_host_daemon_env,
    _discover_local_server_url,
    _ensure_backend,
    _ensure_host_daemon,
    _resolve_attach_server,
    _resolve_host_server,
)
from omnigent.cli import (
    cli as cli_group,
)
from omnigent.host.local_server import LocalServerStartup


@pytest.fixture(autouse=True)
def _stable_current_host_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep daemon-reuse tests independent of the developer's real config."""
    monkeypatch.setattr(cli, "_load_existing_host_id", lambda: "host_abc")


class _Proc:
    """Subprocess stub returned by a patched ``Popen``.

    :param args: Command line passed to ``Popen``.
    :param env: Environment passed to ``Popen``.
    :param _kwargs: Remaining Popen kwargs (stdout/stderr/start_new_session).
    """

    pid = 7777

    def __init__(self, args: list[str], *, env: dict[str, str], **_kwargs: object) -> None:
        self.args = args
        self.env = env


def _patch_daemon_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, captured: dict[str, object]
) -> None:
    """Patch ``_ensure_host_daemon``'s side effects to a tmp pidfile + stub Popen.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temp dir for the host pidfile + daemon logs.
    :param captured: Dict the Popen stub records ``args`` into.
    """
    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")

    def _popen(args: list[str], *, env: dict[str, str], **_kwargs: object) -> _Proc:
        proc = _Proc(args, env=env)
        captured["args"] = args
        captured["env"] = env
        calls = captured.setdefault("calls", [])
        assert isinstance(calls, list)
        calls.append(proc)
        return proc

    monkeypatch.setattr(cli.subprocess, "Popen", _popen)


def _write_daemon_registry_record(
    tmp_path: Path,
    *,
    pid: int,
    target: str,
    mode: str,
    server_url: str | None,
    log_path: str | None = None,
    started_at: int = 100,
    host_id: str | None = "host_abc",
    config_sig: str | None = None,
    resolved_server_url: str | None = None,
) -> None:
    """Write a daemon registry JSON fixture.

    :param tmp_path: Temp directory containing the patched ``host.pid``.
    :param pid: Daemon process id, e.g. ``4242``.
    :param target: Normalized daemon target, e.g.
        ``"https://server.example.com"``.
    :param mode: Daemon mode, either ``"server"`` or ``"local"``.
    :param server_url: Server URL for server mode, e.g.
        ``"https://server.example.com"``.
    :param log_path: Optional daemon log path. A non-``None`` value marks
        the record as background-spawned (eligible for self-healing).
    :param started_at: Registry timestamp.
    :param host_id: Host id owned by the daemon.
    :param config_sig: Config signature the daemon was spawned under, e.g.
        ``"3f9a1c2b4d5e6f70"``, or ``None`` for a legacy record.
    :param resolved_server_url: Concrete local server URL, e.g.
        ``"http://127.0.0.1:8123"``, or ``None``.
    """
    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]
    path = tmp_path / "daemons" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "target": target,
                "mode": mode,
                "server_url": server_url,
                "log_path": log_path,
                "started_at": started_at,
                "host_id": host_id,
                "resolved_server_url": resolved_server_url,
                "config_sig": config_sig,
            },
            sort_keys=True,
        )
        + "\n"
    )


def test_ensure_host_daemon_remote_spawns_server_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A remote URL spawns the daemon with ``--server <url>`` and records it.

    The host pidfile must key on the normalized URL so reuse and the
    local-vs-remote distinction work.
    """
    captured: dict[str, object] = {}
    _patch_daemon_spawn(monkeypatch, tmp_path, captured)

    _ensure_host_daemon("https://example.databricksapps.com/")

    args = captured["args"]
    assert isinstance(args, list)
    assert "--server" in args and "https://example.databricksapps.com/" in args
    assert "--local" not in args
    assert (tmp_path / "host.pid").read_text().splitlines()[1] == (
        "https://example.databricksapps.com"
    )


def test_ensure_host_daemon_local_spawns_local_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``server_url=None`` spawns the daemon with ``--local`` and marks it.

    The pidfile target is the ``"local"`` marker so a later local-mode
    invocation reuses it (and a remote request respawns).
    """
    captured: dict[str, object] = {}
    _patch_daemon_spawn(monkeypatch, tmp_path, captured)

    _ensure_host_daemon(None)

    args = captured["args"]
    assert isinstance(args, list)
    assert "--local" in args
    assert "--server" not in args
    assert (tmp_path / "host.pid").read_text().splitlines()[1] == "local"


def test_ensure_host_daemon_local_inherits_data_dir_and_db_uri(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The local daemon inherits the runtime data-dir + DB URI vars.

    In local mode the daemon owns the local Omnigent server, so it must resolve the
    same config home, data dir, and DB URI the CLI assumes — otherwise the CLI
    reads the local-server pidfile from one dir while the daemon writes it to
    another and discovery times out.
    """
    captured: dict[str, object] = {}
    _patch_daemon_spawn(monkeypatch, tmp_path, captured)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "iso"))
    monkeypatch.setenv("OMNIGENT_DATABASE_URI", "postgresql://u:pw@h/db")

    _ensure_host_daemon(None)

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OMNIGENT_CONFIG_HOME"] == str(tmp_path / "iso")
    assert env["OMNIGENT_DATABASE_URI"] == "postgresql://u:pw@h/db"


def test_build_host_daemon_env_local_preserves_server_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local daemon env carries credentials needed by its Omnigent server.

    The daemon's local server is the process that performs LLM calls, so
    stripping ``OPENAI_*`` here makes default persistent ``omnigent run``
    invocations hang or fail after booting a credential-less server.
    """
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.databricks.com/serving-endpoints")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("OMNIGENT_DATABASE_URI", "postgresql://u:pw@h/db")
    monkeypatch.setenv("GITHUB_TOKEN", "unrelated-github-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "unrelated-aws-secret")

    env = _build_host_daemon_env(server_url=None)
    empty_string_env = _build_host_daemon_env(server_url="")

    assert env["OPENAI_API_KEY"] == "test-key"
    assert env["OPENAI_BASE_URL"] == "https://example.databricks.com/serving-endpoints"
    assert env["ANTHROPIC_API_KEY"] == "test-anthropic-key"
    assert env["OMNIGENT_DATABASE_URI"] == "postgresql://u:pw@h/db"
    assert "GITHUB_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert empty_string_env["OPENAI_API_KEY"] == "test-key"


def test_build_host_daemon_env_local_forwards_bedrock_skip_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLAUDE_CODE_SKIP_BEDROCK_AUTH reaches the local daemon env.

    LiteLLM proxies fronting Bedrock need this flag to disable AWS SigV4
    auth. Without it in the daemon allowlist, ``omni claude`` drops the
    flag and Claude Code falls back to native AWS auth (which fails).
    """
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CODE_SKIP_BEDROCK_AUTH", "1")

    env = _build_host_daemon_env(server_url=None)

    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] == "1"


def test_build_host_daemon_env_remote_strips_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote daemon env remains allowlisted and does not carry LLM keys."""
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.databricks.com/serving-endpoints")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-databricks-token")

    env = _build_host_daemon_env(server_url="https://example.databricksapps.com")

    assert env["PATH"] == "/usr/bin"
    assert "OPENAI_API_KEY" not in env
    assert "OPENAI_BASE_URL" not in env
    assert "ANTHROPIC_API_KEY" not in env
    # Databricks auth is intentionally preserved for the daemon's server auth.
    assert env["DATABRICKS_TOKEN"] == "test-databricks-token"


def test_ensure_host_daemon_reuses_same_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A live daemon for the same target is reused — no respawn."""
    captured: dict[str, object] = {}
    _patch_daemon_spawn(monkeypatch, tmp_path, captured)
    (tmp_path / "host.pid").write_text("4242\nlocal\n")
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)

    _ensure_host_daemon(None)

    # No spawn happened — the existing local daemon was reused.
    assert "args" not in captured


def test_ensure_host_daemon_keeps_other_target_daemons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Starting target B through the CLI does not terminate target A.

    Regression target: the legacy single ``host.pid`` model killed any live
    daemon whose target differed. Multi-server daemon management requires one
    registry entry per target and no cross-target eviction.
    """
    captured: dict[str, object] = {}
    killed: list[int] = []
    _patch_daemon_spawn(monkeypatch, tmp_path, captured)
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append(pid))

    _ensure_host_daemon("https://server-a.example.com")
    _ensure_host_daemon("https://server-b.example.com")

    calls = captured["calls"]
    assert isinstance(calls, list)
    # Two spawn calls prove both server targets got their own daemon; a
    # single-host pidfile regression would terminate/reuse target A.
    assert len(calls) == 2
    assert killed == []


def test_ensure_host_daemon_local_daemon_serves_requested_url_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A live local daemon already serving the requested loopback URL is reused.

    This is the idempotency path that lets claude-native's own
    ``_ensure_host_daemon(base_url)`` (after ``_ensure_backend`` resolved
    local mode) be a no-op instead of tearing the local daemon down to
    respawn an equivalent remote-mode one.
    """
    captured: dict[str, object] = {}
    _patch_daemon_spawn(monkeypatch, tmp_path, captured)
    (tmp_path / "host.pid").write_text("4242\nlocal\n")
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "local_server_url_if_healthy", lambda: "http://127.0.0.1:8123")

    _ensure_host_daemon("http://127.0.0.1:8123")

    assert "args" not in captured  # reused, not respawned


def test_ensure_host_daemon_reuses_healthy_background_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A live background daemon with matching config + online host is reused.

    The healthy fast path: PID alive, config signature matches this
    invocation, and the host reports online — no teardown, no respawn.
    """
    captured: dict[str, object] = {}
    _patch_daemon_spawn(monkeypatch, tmp_path, captured)
    sig = cli.server_config_signature()
    _write_daemon_registry_record(
        tmp_path,
        pid=4242,
        target="local",
        mode="local",
        server_url=None,
        log_path=str(tmp_path / "daemon.log"),
        started_at=1_000_000,
        config_sig=sig,
        resolved_server_url="http://127.0.0.1:8123",
    )
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    # Old enough to be eligible for the tunnel-health check, and online.
    monkeypatch.setattr(cli.time, "time", lambda: 1_000_100.0)
    monkeypatch.setattr(cli, "_daemon_host_online", lambda record, **_kw: True)
    torn_down: list[str] = []
    monkeypatch.setattr(
        cli, "_terminate_host_unit", lambda record, *, reason: torn_down.append(reason)
    )

    _ensure_host_daemon(None)

    assert "args" not in captured  # reused, not respawned
    assert torn_down == []  # healthy daemon not torn down


def test_ensure_host_daemon_respawns_on_host_identity_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A background daemon with a stale host id is torn down + respawned.

    The native terminal path waits for the current config's host id to come
    online. If daemon reuse keeps a process connected as an older host id, that
    wait can only time out.
    """
    captured: dict[str, object] = {}
    _patch_daemon_spawn(monkeypatch, tmp_path, captured)
    _write_daemon_registry_record(
        tmp_path,
        pid=4242,
        target="local",
        mode="local",
        server_url=None,
        log_path=str(tmp_path / "daemon.log"),
        started_at=1_000_000,
        host_id="host_old",
        config_sig=cli.server_config_signature(),
        resolved_server_url="http://127.0.0.1:8123",
    )
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_load_existing_host_id", lambda: "host_new")
    torn_down: list[str] = []
    monkeypatch.setattr(
        cli, "_terminate_host_unit", lambda record, *, reason: torn_down.append(reason)
    )

    _ensure_host_daemon(None)

    assert len(torn_down) == 1 and "identity" in torn_down[0]
    assert "args" in captured


def test_ensure_host_daemon_respawns_on_config_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A background daemon spawned under a different config is torn down + respawned.

    The auth-drift fix at the daemon layer: when the running daemon's
    stamped config signature differs from this invocation's (e.g. the user
    flipped ``OMNIGENT_AUTH_ENABLED``), the unit is torn down and a
    fresh daemon spawned so the new auth mode takes effect.
    """
    captured: dict[str, object] = {}
    _patch_daemon_spawn(monkeypatch, tmp_path, captured)
    _write_daemon_registry_record(
        tmp_path,
        pid=4242,
        target="local",
        mode="local",
        server_url=None,
        log_path=str(tmp_path / "daemon.log"),
        started_at=1_000_000,
        config_sig="stale-signature-0000",
        resolved_server_url="http://127.0.0.1:8123",
    )
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    torn_down: list[str] = []
    monkeypatch.setattr(
        cli, "_terminate_host_unit", lambda record, *, reason: torn_down.append(reason)
    )

    _ensure_host_daemon(None)

    assert len(torn_down) == 1 and "config" in torn_down[0]
    assert "args" in captured  # fresh daemon spawned


def test_ensure_host_daemon_heals_offline_tunnel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A live-but-offline background daemon (zombie) is torn down + respawned.

    The flaky-runs fix: PID alive and config matches, but the host tunnel
    is down (server restart / ungraceful death). Rather than reuse a zombie
    and let the caller poll until timeout, tear the unit down and respawn.
    """
    captured: dict[str, object] = {}
    _patch_daemon_spawn(monkeypatch, tmp_path, captured)
    _write_daemon_registry_record(
        tmp_path,
        pid=4242,
        target="local",
        mode="local",
        server_url=None,
        log_path=str(tmp_path / "daemon.log"),
        started_at=1_000_000,
        config_sig=cli.server_config_signature(),
        resolved_server_url="http://127.0.0.1:8123",
    )
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    # Old enough to be past the min-age grace; tunnel does not recover.
    monkeypatch.setattr(cli.time, "time", lambda: 1_000_100.0)
    monkeypatch.setattr(cli, "_daemon_tunnel_recovers", lambda record, **_kw: False)
    torn_down: list[str] = []
    monkeypatch.setattr(
        cli, "_terminate_host_unit", lambda record, *, reason: torn_down.append(reason)
    )

    _ensure_host_daemon(None)

    assert len(torn_down) == 1 and "offline" in torn_down[0]
    assert "args" in captured  # fresh daemon spawned


def test_ensure_host_daemon_young_offline_daemon_not_torn_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A freshly-spawned daemon still connecting is reused, not torn down.

    Guards against racing a concurrent invocation's just-spawned daemon:
    below the min-age threshold an offline host is assumed to be mid-connect
    and reused (the caller's host-online wait covers the rest).
    """
    captured: dict[str, object] = {}
    _patch_daemon_spawn(monkeypatch, tmp_path, captured)
    _write_daemon_registry_record(
        tmp_path,
        pid=4242,
        target="local",
        mode="local",
        server_url=None,
        log_path=str(tmp_path / "daemon.log"),
        started_at=1_000_000,
        config_sig=cli.server_config_signature(),
        resolved_server_url="http://127.0.0.1:8123",
    )
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    # Younger than _DAEMON_REUSE_MIN_AGE_S → skip the tunnel-health teardown.
    monkeypatch.setattr(cli.time, "time", lambda: 1_000_002.0)

    def _must_not_probe(record: object, **_kw: object) -> bool:
        raise AssertionError("young daemon must not be probed/torn down")

    monkeypatch.setattr(cli, "_daemon_tunnel_recovers", _must_not_probe)
    torn_down: list[str] = []
    monkeypatch.setattr(
        cli, "_terminate_host_unit", lambda record, *, reason: torn_down.append(reason)
    )

    _ensure_host_daemon(None)

    assert torn_down == []
    assert "args" not in captured  # reused despite being offline (still connecting)


def _online_record() -> cli._HostDaemonRecord:
    """Build a local daemon record suitable for host-online probing.

    :returns: A record with a host id and a resolved local server URL.
    """
    return cli._HostDaemonRecord(
        pid=4242,
        target="local",
        mode="local",
        server_url=None,
        log_path="/tmp/daemon.log",
        started_at=1_000_000,
        host_id="host_abc",
        resolved_server_url="http://127.0.0.1:8123",
    )


def test_daemon_host_online_true_when_server_reports_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe returns ``True`` only on a 200 with ``status == "online"``."""
    monkeypatch.setattr(
        cli,
        "_host_http_json",
        lambda **_kw: cli._HostHttpResult(status_code=200, body={"status": "online"}),
    )
    assert cli._daemon_host_online(_online_record()) is True


def test_daemon_host_online_false_when_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host the server reports as offline is not online."""
    monkeypatch.setattr(
        cli,
        "_host_http_json",
        lambda **_kw: cli._HostHttpResult(status_code=200, body={"status": "offline"}),
    )
    assert cli._daemon_host_online(_online_record()) is False


def test_daemon_host_online_false_when_server_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed request (status 0) means the host is not reachable/online."""
    monkeypatch.setattr(
        cli,
        "_host_http_json",
        lambda **_kw: cli._HostHttpResult(status_code=0, body="ConnectError: refused"),
    )
    assert cli._daemon_host_online(_online_record()) is False


def test_daemon_host_online_false_when_no_host_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a known host id there is nothing to probe."""
    monkeypatch.setattr(cli, "_load_existing_host_id", lambda: None)

    def _must_not_call(**_kw: object) -> object:
        raise AssertionError("must not issue an HTTP probe without a host id")

    monkeypatch.setattr(cli, "_host_http_json", _must_not_call)
    record = cli._HostDaemonRecord(
        pid=4242,
        target="local",
        mode="local",
        server_url=None,
        log_path="/tmp/daemon.log",
        started_at=1_000_000,
        host_id=None,
        resolved_server_url="http://127.0.0.1:8123",
    )
    assert cli._daemon_host_online(record) is False


def test_daemon_tunnel_recovers_returns_true_on_immediate_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the host is already online, recovery returns without polling."""
    monkeypatch.setattr(cli, "_daemon_host_online", lambda record, **_kw: True)

    def _must_not_sleep(_s: float) -> None:
        raise AssertionError("must not sleep when already online")

    monkeypatch.setattr(cli.time, "sleep", _must_not_sleep)
    assert cli._daemon_tunnel_recovers(_online_record()) is True


def test_daemon_tunnel_recovers_false_when_never_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistently-offline host fails recovery within the grace window."""
    monkeypatch.setattr(cli, "_daemon_host_online", lambda record, **_kw: False)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    assert cli._daemon_tunnel_recovers(_online_record(), grace_s=0.0) is False


def test_ensure_backend_exits_clean_on_config_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config-drift respawn stops with a clean re-run prompt, not a continue.

    When ``_ensure_host_daemon`` reports the daemon was restarted because its
    auth/profile config changed, ``_ensure_backend`` must not return into the
    in-flight command (the server was just restarted into a new auth mode);
    it exits 0 so the user re-runs against the fresh server.
    """
    monkeypatch.setattr(cli, "_ensure_host_daemon", lambda server: True)
    monkeypatch.setattr(cli, "_discover_local_server_url", lambda: "http://127.0.0.1:8000")
    monkeypatch.setattr(cli, "_update_daemon_resolved_server_url", lambda target, url: None)
    monkeypatch.setattr(
        cli,
        "_host_http_json",
        lambda **_kw: cli._HostHttpResult(
            status_code=200, body={"accounts_enabled": True, "needs_setup": True}
        ),
    )

    with pytest.raises(SystemExit) as exc:
        _ensure_backend(None)
    assert exc.value.code == 0


def test_ensure_backend_continues_when_no_config_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain reuse / heal does NOT exit — the command continues normally."""
    monkeypatch.setattr(cli, "_ensure_host_daemon", lambda server: False)
    monkeypatch.setattr(cli, "_discover_local_server_url", lambda: "http://127.0.0.1:8000")
    monkeypatch.setattr(cli, "_update_daemon_resolved_server_url", lambda target, url: None)

    def _must_not_probe(**_kw: object) -> object:
        raise AssertionError("must not probe /v1/info when config did not change")

    monkeypatch.setattr(cli, "_host_http_json", _must_not_probe)

    assert _ensure_backend(None) == "http://127.0.0.1:8000"


def test_foreground_connect_registers_status_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Foreground ``host`` is visible to status while it runs."""
    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr(cli, "_load_effective_config", dict)
    monkeypatch.setattr(cli, "_load_or_create_host_id", lambda: "host_abc")
    monkeypatch.setattr(cli, "_ensure_databricks_server_auth", lambda server, **kw: None)
    observed: list[cli._HostDaemonRecord] = []

    def _fake_run_host_process(server_url: str) -> None:
        """Capture the foreground registry record during connect execution."""
        observed.extend(cli._list_daemon_records(include_legacy=False))
        assert server_url == "https://server.example.com"

    monkeypatch.setattr("omnigent.host.connect.run_host_process", _fake_run_host_process)

    result = CliRunner().invoke(
        cli_group,
        ["host", "--server", "https://server.example.com"],
    )

    assert result.exit_code == 0, result.output
    assert len(observed) == 1
    assert observed[0].target == "https://server.example.com"
    assert observed[0].pid == cli.os.getpid()
    assert observed[0].host_id == "host_abc"
    assert cli._list_daemon_records(include_legacy=False) == []


def test_foreground_connect_refuses_duplicate_live_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Foreground ``host`` refuses a second live daemon for one server."""
    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr(cli, "_load_effective_config", dict)
    monkeypatch.setattr(cli, "_load_or_create_host_id", lambda: "host_abc")
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: pid == 4242)
    _write_daemon_registry_record(
        tmp_path,
        pid=4242,
        target="https://server.example.com",
        mode="server",
        server_url="https://server.example.com",
    )

    def _unexpected_run_host_process(server_url: str) -> None:
        """Fail if duplicate detection lets the foreground daemon start."""
        raise AssertionError(f"unexpected foreground connect: {server_url}")

    monkeypatch.setattr(
        "omnigent.host.connect.run_host_process",
        _unexpected_run_host_process,
    )

    result = CliRunner().invoke(
        cli_group,
        ["host", "--server", "https://server.example.com/"],
    )

    assert result.exit_code != 0
    assert "already running for this server" in result.output
    assert "pid=4242" in result.output


def _patch_foreground_host_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    run_host_process: Any,
    spawned: bool = True,
) -> None:
    """Stub the local-mode foreground ``host`` dependencies.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temp dir for the host pidfile.
    :param run_host_process: Stub for ``run_host_process`` controlling how
        the daemon "exits" (clean return, ``KeyboardInterrupt``, or
        ``SystemExit``).
    :param spawned: Whether ``ensure_local_omnigent_server`` reports it spawned a
        new server (``True``) or reused an existing one (``False``). The
        Ctrl-C stop-server prompt only fires when ``True``.
    """
    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr(cli, "_load_effective_config", dict)
    monkeypatch.setattr(cli, "_load_or_create_host_id", lambda: "host_abc")
    monkeypatch.setattr(
        cli,
        "ensure_local_omnigent_server",
        lambda: LocalServerStartup(url="http://127.0.0.1:8000", spawned=spawned),
    )
    monkeypatch.setattr("omnigent.host.connect.run_host_process", run_host_process)


def test_foreground_connect_local_prompts_and_stops_server_on_yes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Answering yes at the exit prompt stops the detached local server."""
    _patch_foreground_host_local(monkeypatch, tmp_path, run_host_process=lambda server_url: None)
    monkeypatch.setattr(cli, "local_server_url_if_healthy", lambda: "http://127.0.0.1:8000")
    stopped: list[bool] = []
    monkeypatch.setattr(cli, "stop_local_omnigent_server", lambda: stopped.append(True))

    result = CliRunner().invoke(cli_group, ["host", ""], input="y\n")

    assert result.exit_code == 0, result.output
    assert stopped == [True]
    assert "Stop it too?" in result.output
    assert "Stopped the local server (http://127.0.0.1:8000)." in result.output


def test_foreground_connect_local_prompt_declined_leaves_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Answering no at the exit prompt leaves the server running."""
    _patch_foreground_host_local(monkeypatch, tmp_path, run_host_process=lambda server_url: None)
    monkeypatch.setattr(cli, "local_server_url_if_healthy", lambda: "http://127.0.0.1:8000")
    monkeypatch.setattr(
        cli,
        "stop_local_omnigent_server",
        lambda: pytest.fail("declining must not stop the server"),
    )

    result = CliRunner().invoke(cli_group, ["host", ""], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Left the local server running at http://127.0.0.1:8000." in result.output


def test_foreground_connect_local_prompt_aborted_leaves_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Aborting the prompt (EOF / second Ctrl-C) leaves the server running.

    ``click.confirm`` raises ``click.Abort`` on EOF (non-interactive stdin)
    or a second Ctrl-C. The prompt must treat that as "no" — never stop the
    server and still exit 0 rather than dying with an ``Aborted!`` trace.
    """
    _patch_foreground_host_local(monkeypatch, tmp_path, run_host_process=lambda server_url: None)
    monkeypatch.setattr(cli, "local_server_url_if_healthy", lambda: "http://127.0.0.1:8000")
    monkeypatch.setattr(
        cli,
        "stop_local_omnigent_server",
        lambda: pytest.fail("an aborted prompt must not stop the server"),
    )

    def _raise_abort(*_args: object, **_kwargs: object) -> bool:
        """Stand in for ``click.confirm`` hitting EOF / a second Ctrl-C."""
        raise click.Abort

    # Simulate the abort at the confirm boundary deterministically — empty
    # CliRunner stdin yields the default (False), which is the same path as
    # the ``n`` test, not the Abort branch this test targets.
    monkeypatch.setattr(cli.click, "confirm", _raise_abort)

    result = CliRunner().invoke(cli_group, ["host", ""])

    # Exit 0 (Abort swallowed, no traceback) and the server is left running.
    assert result.exit_code == 0, result.output
    assert "Left the local server running at http://127.0.0.1:8000." in result.output


def test_foreground_connect_local_prompts_after_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Ctrl-C stop (KeyboardInterrupt) still reaches the exit prompt."""

    def _interrupt(server_url: str) -> None:
        """Simulate Ctrl-C stopping the foreground daemon."""
        raise KeyboardInterrupt

    _patch_foreground_host_local(monkeypatch, tmp_path, run_host_process=_interrupt)
    monkeypatch.setattr(cli, "local_server_url_if_healthy", lambda: "http://127.0.0.1:8000")
    monkeypatch.setattr(
        cli,
        "stop_local_omnigent_server",
        lambda: pytest.fail("declining must not stop the server"),
    )

    result = CliRunner().invoke(cli_group, ["host", ""], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Stop it too?" in result.output


def test_foreground_connect_local_no_prompt_when_server_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No prompt fires when no healthy local server is found at exit."""
    _patch_foreground_host_local(monkeypatch, tmp_path, run_host_process=lambda server_url: None)
    monkeypatch.setattr(cli, "local_server_url_if_healthy", lambda: None)
    monkeypatch.setattr(
        cli,
        "stop_local_omnigent_server",
        lambda: pytest.fail("nothing to stop when no server is running"),
    )

    result = CliRunner().invoke(cli_group, ["host", ""], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Stop it too?" not in result.output


def test_foreground_connect_reused_server_omits_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reusing a server we didn't spawn (e.g. ``omnigent server``) skips the prompt.

    Local mode connecting to a server that was already running must NOT offer
    to stop it on Ctrl-C — the user started it independently, so killing it
    would be surprising.
    """
    _patch_foreground_host_local(
        monkeypatch,
        tmp_path,
        run_host_process=lambda server_url: None,
        spawned=False,
    )
    # A healthy server exists, but since we reused it the prompt must not even
    # probe / fire — fail loudly if it tries to stop someone else's server.
    monkeypatch.setattr(
        cli,
        "local_server_url_if_healthy",
        lambda: pytest.fail("reused-server connect must not probe the stop prompt"),
    )
    monkeypatch.setattr(
        cli,
        "stop_local_omnigent_server",
        lambda: pytest.fail("must never stop a server we did not spawn"),
    )

    result = CliRunner().invoke(cli_group, ["host", ""], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Stop it too?" not in result.output


def test_foreground_connect_connection_failure_skips_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A connection failure (SystemExit) does not prompt over the error."""

    def _fail(server_url: str) -> None:
        """Simulate a permanent connection failure exiting non-zero."""
        raise SystemExit(1)

    _patch_foreground_host_local(monkeypatch, tmp_path, run_host_process=_fail)
    monkeypatch.setattr(
        cli,
        "local_server_url_if_healthy",
        lambda: pytest.fail("a failed connect must not probe / prompt"),
    )

    result = CliRunner().invoke(cli_group, ["host", ""])

    assert result.exit_code == 1


def test_foreground_connect_remote_omits_local_server_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Remote-mode ``host`` never probes for or prompts about a local server."""
    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr(cli, "_load_effective_config", dict)
    monkeypatch.setattr(cli, "_load_or_create_host_id", lambda: "host_abc")
    monkeypatch.setattr(cli, "_ensure_databricks_server_auth", lambda server, **kw: None)
    monkeypatch.setattr(
        cli,
        "local_server_url_if_healthy",
        lambda: pytest.fail("remote mode must not probe the local server"),
    )
    monkeypatch.setattr(
        "omnigent.host.connect.run_host_process",
        lambda server_url: None,
    )

    result = CliRunner().invoke(cli_group, ["host", "--server", "https://server.example.com"])

    assert result.exit_code == 0, result.output
    assert "Stop it too?" not in result.output


def test_host_status_json_reports_daemon_host_and_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``host status --json`` includes daemon, host, runner, and sessions."""
    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    _write_daemon_registry_record(
        tmp_path,
        pid=4242,
        target="https://server.example.com",
        mode="server",
        server_url="https://server.example.com",
        log_path="/tmp/daemon.log",
    )

    runner_status_calls: list[str] = []

    def _fake_http_json(**kwargs: object) -> cli._HostHttpResult:
        """Return host/session fixtures keyed by request path."""
        path = kwargs["path"]
        if path == "/v1/hosts/host_abc":
            return cli._HostHttpResult(status_code=200, body={"status": "online"})
        if path == "/v1/sessions":
            return cli._HostHttpResult(
                status_code=200,
                body={
                    "data": [
                        {
                            "id": "conv_owned",
                            "host_id": "host_abc",
                            "status": "running",
                            "runner_id": "runner_abc",
                            "title": "owned",
                        },
                        {
                            "id": "conv_other",
                            "host_id": "host_other",
                            "status": "idle",
                        },
                    ]
                },
            )
        if path == "/v1/runners/runner_abc/status":
            runner_status_calls.append("runner_abc")
            return cli._HostHttpResult(
                status_code=200,
                body={"runner_id": "runner_abc", "online": True},
            )
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(cli, "_host_http_json", _fake_http_json)

    result = CliRunner().invoke(cli_group, ["host", "status", "--json"])

    assert result.exit_code == 0, result.output
    assert '"target": "https://server.example.com"' in result.output
    assert '"host_status": "online"' in result.output
    assert '"id": "conv_owned"' in result.output
    assert '"runner_online": true' in result.output
    assert '"id": "conv_other"' not in result.output
    assert runner_status_calls == ["runner_abc"]


def test_host_status_reports_unreachable_daemon_without_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``host status`` renders per-daemon connection failures."""
    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    _write_daemon_registry_record(
        tmp_path,
        pid=4242,
        target="https://bad.example.invalid",
        mode="server",
        server_url="https://bad.example.invalid",
    )

    def _fake_http_json(**kwargs: object) -> cli._HostHttpResult:
        """Return the local-failure shape produced by ``_host_http_json``."""
        assert kwargs["path"] in {"/v1/hosts/host_abc", "/v1/sessions"}
        return cli._HostHttpResult(
            status_code=0,
            body="ConnectError: nodename nor servname provided, or not known",
        )

    monkeypatch.setattr(cli, "_host_http_json", _fake_http_json)

    result = CliRunner().invoke(cli_group, ["host", "status"])

    assert result.exit_code == 0, result.output
    assert "host status failed: ConnectError" in result.output
    assert "mode=server" in result.output
    assert "pid=4242" in result.output
    assert "Traceback" not in result.output


def test_host_status_wide_terminal_shows_full_session_and_runner_ids() -> None:
    """Wide ``host status`` tables preserve full session and runner ids."""
    session_id = "conv_1234567890abcdef1234567890abcdef12345678"
    runner_id = "runner_token_1234567890abcdef1234567890abcdef12345678"
    console = Console(width=180, record=True, color_system=None)

    cli._add_host_payload_sessions_table(
        console,
        {
            "sessions": [
                {
                    "id": session_id,
                    "status": "idle",
                    "runner_id": runner_id,
                    "runner_online": True,
                    "title": "wide terminal",
                }
            ]
        },
    )

    rendered = console.export_text()
    assert session_id in rendered
    assert runner_id in rendered


def test_host_sessions_subcommand_is_removed() -> None:
    """``host sessions`` is not a separate inspection surface."""
    result = CliRunner().invoke(cli_group, ["host", "sessions"])

    assert result.exit_code != 0
    assert "No such command 'sessions'" in result.output


def test_host_stop_stops_sessions_before_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``connect stop`` posts stop_session before terminating the daemon."""
    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    _write_daemon_registry_record(
        tmp_path,
        pid=4242,
        target="https://server.example.com",
        mode="server",
        server_url="https://server.example.com",
    )
    events: list[tuple[str, str]] = []

    def _fake_http_json(**kwargs: object) -> cli._HostHttpResult:
        """Record lifecycle requests and return minimal Omnigent responses."""
        method = str(kwargs["method"])
        path = str(kwargs["path"])
        events.append((method, path))
        if method == "GET" and path == "/v1/sessions":
            return cli._HostHttpResult(
                status_code=200,
                body={
                    "data": [
                        {
                            "id": "conv_owned",
                            "host_id": "host_abc",
                            "status": "running",
                            "runner_id": "runner_abc",
                        }
                    ]
                },
            )
        if method == "POST" and path == "/v1/sessions/conv_owned/events":
            return cli._HostHttpResult(status_code=200, body={"queued": False})
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(cli, "_host_http_json", _fake_http_json)

    def _fake_terminate(record: cli._HostDaemonRecord, *, force: bool) -> None:
        """Record daemon termination without signaling a real process."""
        del force
        events.append(("TERM", record.target))

    monkeypatch.setattr(cli, "_terminate_daemon", _fake_terminate)

    result = CliRunner().invoke(
        cli_group,
        ["host", "stop", "--server", "https://server.example.com"],
    )

    assert result.exit_code == 0, result.output
    assert events == [
        ("GET", "/v1/sessions"),
        ("POST", "/v1/sessions/conv_owned/events"),
        ("TERM", "https://server.example.com"),
    ]
    assert "sessions_stopped=1" in result.output


def test_host_stop_daemon_only_skips_session_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``connect stop --daemon-only`` terminates without HTTP session calls."""
    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    _write_daemon_registry_record(
        tmp_path,
        pid=4242,
        target="https://server.example.com",
        mode="server",
        server_url="https://server.example.com",
    )
    terminated: list[str] = []
    monkeypatch.setattr(
        cli,
        "_host_http_json",
        lambda **kwargs: pytest.fail(f"unexpected HTTP call: {kwargs}"),
    )
    monkeypatch.setattr(
        cli,
        "_terminate_daemon",
        lambda record, *, force: terminated.append(record.target),
    )

    result = CliRunner().invoke(
        cli_group,
        ["host", "stop", "--server", "https://server.example.com", "--daemon-only"],
    )

    assert result.exit_code == 0, result.output
    assert terminated == ["https://server.example.com"]


def test_host_stop_session_stops_only_named_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``connect stop-session`` posts stop events only for requested ids."""
    stopped: list[str] = []
    monkeypatch.setattr(cli, "_load_effective_config", dict)

    def _fake_stop_session(*, base_url: str, session_id: str) -> None:
        """Record requested session stops without making HTTP calls."""
        assert base_url == "https://server.example.com"
        stopped.append(session_id)

    monkeypatch.setattr(cli, "_stop_session_on_server", _fake_stop_session)

    result = CliRunner().invoke(
        cli_group,
        [
            "host",
            "--server",
            "https://server.example.com",
            "stop-session",
            "conv_a",
            "conv_b",
        ],
    )

    assert result.exit_code == 0, result.output
    assert stopped == ["conv_a", "conv_b"]


def test_ensure_backend_remote_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """A remote URL ensures a daemon for it and returns the normalized URL."""
    calls: list[str | None] = []
    monkeypatch.setattr(cli, "_ensure_host_daemon", lambda s: calls.append(s))
    # Identity normalization: the workspace-URL expansion probes the
    # network and has dedicated tests.
    monkeypatch.setattr(cli, "_workspace_api_server_url", lambda server: server.rstrip("/"))
    monkeypatch.setattr(cli, "_ensure_databricks_server_auth", lambda server: None)

    result = _ensure_backend("https://example.databricksapps.com/")

    assert result == "https://example.databricksapps.com"
    # The daemon receives the normalized (slash-stripped) URL so its
    # pidfile target matches what later commands compute.
    assert calls == ["https://example.databricksapps.com"]


def test_ensure_backend_local_discovers_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """No URL ensures a ``--local`` daemon and returns the discovered URL.

    The CLI does not start the server itself — it discovers the URL the
    daemon's server published. ``_ensure_host_daemon`` must be called with
    ``None`` (local mode).
    """
    calls: list[str | None] = []
    monkeypatch.setattr(cli, "_ensure_host_daemon", lambda s: calls.append(s))
    monkeypatch.setattr(cli, "_discover_local_server_url", lambda: "http://127.0.0.1:8123")

    assert _ensure_backend(None) == "http://127.0.0.1:8123"
    assert _ensure_backend("") == "http://127.0.0.1:8123"
    assert calls == [None, None]


def test_ensure_backend_defaults_scheme_https(monkeypatch: pytest.MonkeyPatch) -> None:
    """A schemeless ``--server`` URL is defaulted to https before expansion.

    Covers run / claude / codex / chat, which all resolve ``--server``
    through ``_ensure_backend``; the guide hands out schemeless URLs.
    """
    seen: list[str] = []
    monkeypatch.setattr(cli, "_ensure_host_daemon", lambda s: False)
    monkeypatch.setattr(cli, "_workspace_api_server_url", _recording_expander(seen))
    monkeypatch.setattr(cli, "_ensure_databricks_server_auth", lambda server: None)

    result = _ensure_backend("dbc-x.cloud.databricks.com/omnigent")

    # Scheme defaulted to https before the workspace expansion ran.
    assert seen == ["https://dbc-x.cloud.databricks.com/omnigent"]
    assert result == _expand_marker("https://dbc-x.cloud.databricks.com/omnigent")


def test_discover_local_server_url_returns_when_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery returns as soon as the local server answers health."""
    monkeypatch.setattr(cli, "local_server_url_if_healthy", lambda: "http://127.0.0.1:8123")
    assert _discover_local_server_url(timeout=1.0) == "http://127.0.0.1:8123"


def test_discover_local_server_url_raises_when_daemon_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the daemon exits before its server is ready, fail loud (not hang)."""
    monkeypatch.setattr(cli, "local_server_url_if_healthy", lambda: None)
    monkeypatch.setattr(cli, "_host_daemon_alive", lambda: False)
    with pytest.raises(click.ClickException, match="exited before"):
        _discover_local_server_url(timeout=5.0)


def _fake_run_claude_native_capture(captured: dict[str, object]) -> Any:
    """Build a ``run_claude_native`` stub that records its kwargs.

    :param captured: Dict the stub writes recorded kwargs into.
    :returns: Stub callable that accepts arbitrary kwargs.
    """

    def _stub(**kwargs: object) -> None:
        captured.update(kwargs)

    return _stub


def test_claude_command_routes_server_through_ensure_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``omnigent claude --server ""`` resolves via ``_ensure_backend``.

    The empty/local value must be turned into the concrete daemon-backed URL
    and passed to ``run_claude_native`` — never forwarded raw.
    """
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr(
        "omnigent.cli._ensure_backend",
        lambda server: "http://127.0.0.1:8123",
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native",
        _fake_run_claude_native_capture(captured),
    )

    result = CliRunner().invoke(cli_group, ["claude", "--server", ""])

    assert result.exit_code == 0, result.output
    assert captured["server"] == "http://127.0.0.1:8123"


def _capture_run_chat(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Patch ``run_chat`` to record kwargs and return the capture dict.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: Dict populated with ``run_chat`` kwargs on invocation.
    """
    captured: dict[str, object] = {}

    def _stub(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.chat.run_chat", _stub)
    return captured


def test_run_reads_server_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` uses ``server`` from config when ``--server`` is omitted.

    Regression: ``run`` previously read only ``auto_open_conversation``
    from config and dropped ``server`` / ``model``, so a configured
    default server was silently ignored (unlike ``run``). The value must
    reach ``run_chat`` as ``server_url``.
    """
    monkeypatch.setattr(
        "omnigent.cli._load_effective_config",
        lambda: {
            "server": "https://config-default.example.com",
            "model": "databricks-claude-sonnet-4-6",
        },
    )
    captured = _capture_run_chat(monkeypatch)

    result = CliRunner().invoke(cli_group, ["run", "tests/resources/examples/hello_world.yaml"])

    assert result.exit_code == 0, result.output
    assert captured["server_url"] == "https://config-default.example.com"
    assert captured["model"] == "databricks-claude-sonnet-4-6"


def test_run_explicit_server_overrides_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit ``--server`` wins over the configured default."""
    monkeypatch.setattr(
        "omnigent.cli._load_effective_config",
        lambda: {"server": "https://config-default.example.com"},
    )
    captured = _capture_run_chat(monkeypatch)

    result = CliRunner().invoke(
        cli_group,
        [
            "run",
            "tests/resources/examples/hello_world.yaml",
            "--server",
            "https://explicit.example.com",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["server_url"] == "https://explicit.example.com"


# ── Databricks-fronted server auth pre-flight ───────────────────────


def _databricks_probe_response(status_code: int) -> object:
    """Build a real httpx.Response shaped like the Apps edge answer.

    :param status_code: ``200`` for an authenticated probe, ``302`` for
        the edge's OAuth redirect.
    :returns: A real :class:`httpx.Response` so the production header
        and redirect parsing run for real.
    """
    import httpx

    headers = (
        {"location": ("https://example.databricks.com/oidc/oauth2/v2.0/authorize?client_id=x")}
        if status_code == 302
        else {}
    )
    return httpx.Response(
        status_code,
        headers=headers,
        request=httpx.Request("GET", "https://myapp-1234.aws.databricksapps.com/v1/me"),
    )


def _patch_auth_preflight(
    monkeypatch: pytest.MonkeyPatch,
    *,
    probe_status: int,
    tty: bool,
) -> list[str]:
    """Wire the pre-flight's collaborators for one scripted run.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param probe_status: Status the ``/v1/me`` probe answers with.
    :param tty: What ``sys.stdin.isatty()`` reports.
    :returns: Capture list of ``_databricks_login`` invocations
        (``"<server> <workspace>"`` strings).
    """
    import httpx

    monkeypatch.setattr(
        "omnigent.chat._remote_headers",
        lambda server_url=None: {},
    )
    monkeypatch.setattr(httpx, "get", lambda url, **kw: _databricks_probe_response(probe_status))
    monkeypatch.setattr(cli, "_workspace_api_server_url", lambda server: server.rstrip("/"))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: tty)
    login_calls: list[str] = []

    def _capture_login(server: str, workspace_host: str, org_id: str | None = None) -> None:
        login_calls.append(f"{server} {workspace_host}")

    monkeypatch.setattr(cli, "_databricks_login", _capture_login)
    return login_calls


def test_ensure_backend_databricks_preflight_runs_login_on_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unauthenticated Databricks-fronted server triggers the login flow.

    Without this, the run continues and dies much later in session-create
    with an opaque "non-JSON response (status=302)" traceback.
    """
    login_calls = _patch_auth_preflight(monkeypatch, probe_status=302, tty=True)
    monkeypatch.setattr(cli, "_ensure_host_daemon", lambda server: False)

    result = _ensure_backend("https://myapp-1234.aws.databricksapps.com/")

    # The login flow ran for the probed server + parsed workspace, then
    # the run continued normally with the normalized URL.
    assert login_calls == [
        "https://myapp-1234.aws.databricksapps.com https://example.databricks.com"
    ]
    assert result == "https://myapp-1234.aws.databricksapps.com"


def test_ensure_backend_databricks_preflight_hints_headless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headless invocations get the exact login command, not a browser."""
    login_calls = _patch_auth_preflight(monkeypatch, probe_status=302, tty=False)
    monkeypatch.setattr(cli, "_ensure_host_daemon", lambda server: False)

    with pytest.raises(click.ClickException) as exc:
        _ensure_backend("https://myapp-1234.aws.databricksapps.com")

    assert "omnigent login https://myapp-1234.aws.databricksapps.com" in str(exc.value)
    # No browser flow attempted off-TTY.
    assert login_calls == []


def test_ensure_backend_databricks_preflight_skips_when_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 probe (valid creds / header mode) never invokes login."""
    login_calls = _patch_auth_preflight(monkeypatch, probe_status=200, tty=True)
    monkeypatch.setattr(cli, "_ensure_host_daemon", lambda server: False)

    result = _ensure_backend("https://myapp-1234.aws.databricksapps.com")

    assert login_calls == []
    assert result == "https://myapp-1234.aws.databricksapps.com"


# ── Foreground ``host`` auth pre-flight ─────────────────────────────
#
# ``host`` runs the same Databricks sign-in pre-flight ``run`` does before
# connecting to a remote server, but exposes ``--non-interactive`` so a
# scripted invocation keeps the old fail-loud behavior instead of launching
# the browser login. ``CliRunner`` swaps ``sys.stdin`` for a non-TTY stream,
# so the auto-login-on-TTY branch is covered by the direct pre-flight test
# below; the ``host`` wiring is asserted with a capturing pre-flight stub.

_HOST_DATABRICKS_SERVER = "https://myapp-1234.aws.databricksapps.com"


def test_databricks_preflight_non_interactive_overrides_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``non_interactive=True`` fails with the login hint even on a TTY.

    Without the override an un-authed Databricks server would launch the
    browser login on a TTY (covered by
    ``test_ensure_backend_databricks_preflight_runs_login_on_tty``); the
    flag is what lets ``host`` stay scripted.
    """
    login_calls = _patch_auth_preflight(monkeypatch, probe_status=302, tty=True)

    with pytest.raises(click.ClickException) as exc:
        cli._ensure_databricks_server_auth(_HOST_DATABRICKS_SERVER, non_interactive=True)

    assert f"omnigent login {_HOST_DATABRICKS_SERVER}" in str(exc.value)
    # The browser login never ran despite the TTY.
    assert login_calls == []


def _patch_foreground_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> list[str]:
    """Stub the foreground-host plumbing and capture connect targets.

    Covers both local and remote ``host`` invocations: the local server
    bring-up is stubbed so ``host ""`` reaches ``run_host_process`` too.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temp dir for the host pidfile.
    :returns: Capture list of ``run_host_process`` server URLs.
    """
    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr(cli, "_load_effective_config", dict)
    monkeypatch.setattr(cli, "_load_or_create_host_id", lambda: "host_abc")
    monkeypatch.setattr(
        cli,
        "ensure_local_omnigent_server",
        lambda: LocalServerStartup(url="http://127.0.0.1:8000", spawned=True),
    )
    # No healthy local server after exit → the Ctrl-C stop prompt stays quiet.
    monkeypatch.setattr(cli, "local_server_url_if_healthy", lambda: None)
    connected: list[str] = []
    monkeypatch.setattr(
        "omnigent.host.connect.run_host_process",
        lambda server_url: connected.append(server_url),
    )
    return connected


def _capture_preflight(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """Replace the auth pre-flight with a capturing no-op.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: Capture list of ``(server, non_interactive)`` pre-flight calls.
    """
    calls: list[tuple[str, bool]] = []

    def _capture(server: str, *, non_interactive: bool = False) -> None:
        calls.append((server, non_interactive))

    monkeypatch.setattr(cli, "_ensure_databricks_server_auth", _capture)
    return calls


def test_host_remote_runs_auth_preflight_before_connect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Remote ``host`` runs the sign-in pre-flight, then connects."""
    preflight = _capture_preflight(monkeypatch)
    connected = _patch_foreground_host(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli_group, ["host", "--server", _HOST_DATABRICKS_SERVER])

    assert result.exit_code == 0, result.output
    assert preflight == [(_HOST_DATABRICKS_SERVER, False)]
    assert connected == [_HOST_DATABRICKS_SERVER]


def test_host_non_interactive_flag_forwarded_to_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--non-interactive`` reaches the pre-flight as ``non_interactive=True``."""
    preflight = _capture_preflight(monkeypatch)
    _patch_foreground_host(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        cli_group, ["host", "--server", _HOST_DATABRICKS_SERVER, "--non-interactive"]
    )

    assert result.exit_code == 0, result.output
    assert preflight == [(_HOST_DATABRICKS_SERVER, True)]


def test_host_non_interactive_flag_positional_shorthand(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``omnigent host <url> --non-interactive`` parses the flag with the shorthand."""
    preflight = _capture_preflight(monkeypatch)
    _patch_foreground_host(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli_group, ["host", _HOST_DATABRICKS_SERVER, "--non-interactive"])

    assert result.exit_code == 0, result.output
    assert preflight == [(_HOST_DATABRICKS_SERVER, True)]


def test_host_local_skips_auth_preflight(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Local-mode ``host ""`` never runs the remote sign-in pre-flight."""
    preflight = _capture_preflight(monkeypatch)
    connected = _patch_foreground_host(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli_group, ["host", ""])

    assert result.exit_code == 0, result.output
    assert preflight == []
    assert connected == ["http://127.0.0.1:8000"]


def test_host_remote_preflight_hints_headless(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Un-authed Databricks ``host`` (no TTY) fails with the login hint, never connecting."""
    login_calls = _patch_auth_preflight(monkeypatch, probe_status=302, tty=False)
    connected = _patch_foreground_host(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli_group, ["host", "--server", _HOST_DATABRICKS_SERVER])

    assert result.exit_code != 0
    assert f"omnigent login {_HOST_DATABRICKS_SERVER}" in result.output
    # Pre-flight bailed: no browser login and no connect.
    assert login_calls == []
    assert connected == []


def test_host_remote_preflight_skips_when_authenticated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 200 probe (valid creds / header mode) connects without a login flow."""
    login_calls = _patch_auth_preflight(monkeypatch, probe_status=200, tty=False)
    connected = _patch_foreground_host(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli_group, ["host", "--server", _HOST_DATABRICKS_SERVER])

    assert result.exit_code == 0, result.output
    assert login_calls == []
    assert connected == [_HOST_DATABRICKS_SERVER]


# ── Workspace-URL expansion for attach / resume / host ──────────────
#
# ``run`` / ``claude`` / ``codex`` expand a bare Databricks workspace URL to
# its ``/api/2.0/omnigent`` mount via ``_ensure_backend`` (covered above);
# ``attach``, ``resume``, and the ``host`` subcommands resolve ``--server``
# on their own paths and must route through the same expansion. The
# expansion itself probes the network and is tested in
# ``test_login_databricks.py`` — here we stub it to a recognizable
# transform and assert each resolver actually calls it.


def _expand_marker(server: str) -> str:
    """Stand in for ``_workspace_api_server_url`` with a visible transform.

    :param server: The URL the resolver hands to the expansion.
    :returns: ``server`` with the API mount appended, so a test can tell
        an expanded result apart from a passed-through one.
    """
    return f"{server.rstrip('/')}/api/2.0/omnigent"


def _recording_expander(seen: list[str]) -> Callable[[str], str]:
    """Build a ``_workspace_api_server_url`` stub that records its input.

    :param seen: List the stub appends each received URL to, so a test
        can assert the resolver expanded the bare URL (not a pre-pathed one).
    :returns: Callable that records ``server`` then returns its expansion.
    """

    def _expand(server: str) -> str:
        seen.append(server)
        return _expand_marker(server)

    return _expand


def test_resolve_attach_server_expands_explicit_workspace_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``--server`` workspace URL is expanded to its API mount.

    Regression: ``attach`` returned the bare URL, so ``/v1/sessions/{id}``
    hit the workspace web app and 404'd instead of the omnigent API.
    """
    seen: list[str] = []
    monkeypatch.setattr(cli, "_workspace_api_server_url", _recording_expander(seen))

    result = _resolve_attach_server("https://ws.example.net/", configured_server=None)

    assert result == "https://ws.example.net/api/2.0/omnigent"
    assert seen == ["https://ws.example.net"]


def test_resolve_attach_server_expands_configured_workspace_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured ``server`` default is expanded just like ``--server``."""
    monkeypatch.setattr(cli, "_workspace_api_server_url", _expand_marker)

    result = _resolve_attach_server(None, configured_server="https://ws.example.net")

    assert result == "https://ws.example.net/api/2.0/omnigent"


def test_resolve_attach_server_local_fallback_not_expanded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local-server fallback returns a concrete URL without expansion.

    The background server already publishes a loopback URL; routing it
    through the network probe would be pointless work.
    """
    monkeypatch.setattr(
        cli,
        "_workspace_api_server_url",
        lambda s: pytest.fail("local fallback must not be expanded"),
    )
    monkeypatch.setattr(cli, "local_server_url_if_healthy", lambda: "http://127.0.0.1:8123/")

    assert _resolve_attach_server(None, configured_server=None) == "http://127.0.0.1:8123"


def test_resolve_attach_server_defaults_scheme_https(monkeypatch: pytest.MonkeyPatch) -> None:
    """``attach --server <ws>/omnigent`` (no scheme) is defaulted to https."""
    seen: list[str] = []
    monkeypatch.setattr(cli, "_workspace_api_server_url", _recording_expander(seen))

    result = _resolve_attach_server("dbc-x.cloud.databricks.com/omnigent", configured_server=None)

    assert seen == ["https://dbc-x.cloud.databricks.com/omnigent"]
    assert result == _expand_marker("https://dbc-x.cloud.databricks.com/omnigent")


def test_resolve_host_server_expands_explicit_workspace_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``host`` subcommands expand a bare ``--server`` workspace URL.

    The daemon is registered under the expanded ``/api/2.0/omnigent`` URL,
    so the registry lookup must expand too or it never matches a daemon
    that ``run`` / ``host`` started.
    """
    seen: list[str] = []
    monkeypatch.setattr(cli, "_workspace_api_server_url", _recording_expander(seen))

    result = _resolve_host_server("https://ws.example.net/")

    assert result == "https://ws.example.net/api/2.0/omnigent"
    assert seen == ["https://ws.example.net"]


def test_resolve_host_server_reads_config_and_expands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ``--server``, the configured default is read and expanded."""
    monkeypatch.setattr(
        cli, "_load_effective_config", lambda: {"server": "https://ws.example.net"}
    )
    monkeypatch.setattr(cli, "_workspace_api_server_url", _expand_marker)

    assert _resolve_host_server(None) == "https://ws.example.net/api/2.0/omnigent"


def test_resolve_host_server_none_stays_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CLI value and no configured default stays local (``None``)."""
    monkeypatch.setattr(cli, "_load_effective_config", dict)
    monkeypatch.setattr(
        cli, "_workspace_api_server_url", lambda s: pytest.fail("nothing to expand")
    )

    assert _resolve_host_server(None) is None


def test_resolve_host_server_defaults_scheme_and_accepts_omnigent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``host`` subcommands accept a schemeless ``/omnigent`` workspace URL.

    The internal user guide's web URL omits the scheme and ends in
    ``/omnigent``; host must default it to https before expansion, just
    like ``omnigent login``.
    """
    seen: list[str] = []
    monkeypatch.setattr(cli, "_workspace_api_server_url", _recording_expander(seen))

    result = _resolve_host_server("dbc-x.cloud.databricks.com/omnigent")

    # Scheme defaulted to https before the expansion saw the URL.
    assert seen == ["https://dbc-x.cloud.databricks.com/omnigent"]
    assert result == _expand_marker("https://dbc-x.cloud.databricks.com/omnigent")


def test_host_command_defaults_scheme_and_accepts_omnigent_web_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``omnigent host --server <ws>/omnigent`` (no scheme) normalizes before connect.

    Pasting the guide's web URL (schemeless, ``/omnigent`` suffix) must
    default to https and expand to the API mount, not connect to the raw
    input.
    """
    monkeypatch.setattr(cli, "_HOST_PID_PATH", tmp_path / "host.pid")
    monkeypatch.setattr(cli, "_load_effective_config", dict)
    monkeypatch.setattr(cli, "_load_or_create_host_id", lambda: "host_abc")
    monkeypatch.setattr(cli, "_ensure_databricks_server_auth", lambda server, **kw: None)
    seen: list[str] = []
    monkeypatch.setattr(cli, "_workspace_api_server_url", _recording_expander(seen))
    observed: list[str] = []
    monkeypatch.setattr(
        "omnigent.host.connect.run_host_process",
        lambda server_url: observed.append(server_url),
    )

    result = CliRunner().invoke(
        cli_group, ["host", "--server", "dbc-x.cloud.databricks.com/omnigent"]
    )

    assert result.exit_code == 0, result.output
    # Scheme defaulted to https before the workspace expansion ran.
    assert seen == ["https://dbc-x.cloud.databricks.com/omnigent"]
    # The foreground connect targeted the expanded API-mount URL.
    assert observed == [_expand_marker("https://dbc-x.cloud.databricks.com/omnigent")]


def test_resume_command_expands_server_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """``omnigent resume <id> --server <workspace>`` expands before dispatch.

    Regression: ``resume`` forwarded the bare URL, so its remote picker
    and wrapper-label lookups 404'd against the workspace web app.
    """
    monkeypatch.setattr(cli, "_workspace_api_server_url", _expand_marker)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "omnigent.resume_dispatch.run_resume",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(
        cli_group, ["resume", "conv_abc123", "--server", "https://ws.example.net"]
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "target": "conv_abc123",
        "server": "https://ws.example.net/api/2.0/omnigent",
    }


def test_resume_command_without_server_skips_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--server``, resume forwards ``None`` and never probes."""
    monkeypatch.setattr(
        cli, "_workspace_api_server_url", lambda s: pytest.fail("nothing to expand")
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "omnigent.resume_dispatch.run_resume",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(cli_group, ["resume", "conv_abc123"])

    assert result.exit_code == 0, result.output
    assert captured == {"target": "conv_abc123", "server": None}


def test_resume_command_defaults_scheme_https(monkeypatch: pytest.MonkeyPatch) -> None:
    """``omnigent resume --server <ws>/omnigent`` (no scheme) is defaulted to https."""
    seen: list[str] = []
    monkeypatch.setattr(cli, "_workspace_api_server_url", _recording_expander(seen))
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "omnigent.resume_dispatch.run_resume",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(
        cli_group,
        ["resume", "conv_abc123", "--server", "dbc-x.cloud.databricks.com/omnigent"],
    )

    assert result.exit_code == 0, result.output
    assert seen == ["https://dbc-x.cloud.databricks.com/omnigent"]
    assert captured["server"] == _expand_marker("https://dbc-x.cloud.databricks.com/omnigent")
