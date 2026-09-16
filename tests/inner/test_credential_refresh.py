from __future__ import annotations

import base64
import http.server
import socketserver
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.errors import OmnigentError
from omnigent.inner.credential_proxy import (
    RefreshingSecretProvider,
    prepare_credential_proxy_runtime,
)
from omnigent.inner.datamodel import (
    CredentialProxyEntry,
    CredentialProxySpec,
    CredentialSourceSpec,
    OSEnvSandboxSpec,
    OSEnvSpec,
)
from omnigent.inner.egress.proxy import EgressProxy
from omnigent.inner.sandbox import SandboxPolicy, resolve_sandbox
from omnigent.spec.parser import _parse_credential_proxy


@pytest.fixture
def sandbox(tmp_path: Path) -> SandboxPolicy:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=[],
        write_roots=[workspace],
        write_files=[],
        allow_network=False,
    )


@pytest.fixture
def broker(short_tmp_parent: Path) -> Iterator[tuple[Path, dict[str, object]]]:
    state: dict[str, object] = {"status": 200, "body": b"initial", "calls": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            assert self.path == "/token"
            state["calls"] = int(str(state["calls"])) + 1
            payload = state["body"]
            assert isinstance(payload, bytes)
            self.send_response(int(str(state["status"])))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    path = short_tmp_parent / "broker.sock"
    with socketserver.UnixStreamServer(str(path), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield path, state
        finally:
            server.shutdown()
            thread.join()


def test_runtime_auth_headers_pick_up_rotated_file_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox: SandboxPolicy
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("first-token")
    source: dict[str, object] = {"refresh_interval_seconds": 60, "file": str(token_file)}
    spec = _parse_credential_proxy([{"type": "gh_basic", "source": source}])
    clock = Mock(return_value=0.0)
    monkeypatch.setattr(
        "omnigent.inner.credential_proxy.RefreshingSecretProvider",
        partial(RefreshingSecretProvider, clock=clock),
    )
    runtime = prepare_credential_proxy_runtime(spec, parent_env={}, sandbox=sandbox)
    placeholders = dict(runtime.helper_env_updates)
    token_file.write_text("second-token")

    def auth_values() -> set[str]:
        return {EgressProxy._format_real_auth(rule) for rule in runtime.rewrites}

    first_basic = base64.b64encode(b"x-access-token:first-token").decode()
    assert auth_values() == {"token first-token", f"Basic {first_basic}"}
    clock.return_value = 60.0
    second_basic = base64.b64encode(b"x-access-token:second-token").decode()
    assert auth_values() == {"token second-token", f"Basic {second_basic}"}
    assert runtime.helper_env_updates == placeholders
    assert all(value.startswith("oa_cred_") for value in placeholders.values())
    assert all(rule.real_secret is None for rule in runtime.rewrites)


def test_failed_refresh_retries_without_reusing_cached_secret(
    tmp_path: Path, sandbox: SandboxPolicy
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("initial")
    clock = Mock(return_value=0.0)
    provider = RefreshingSecretProvider(
        CredentialSourceSpec(kind="file", path=str(token_file), refresh_interval_seconds=60),
        parent_env={},
        sandbox=sandbox,
        clock=clock,
    )
    assert provider.resolve() == "initial"
    clock.return_value = 60.0
    token_file.unlink()
    with pytest.raises(ValueError, match="does not exist"):
        provider.resolve()
    token_file.write_text("")
    with pytest.raises(ValueError, match="empty"):
        provider.resolve()
    token_file.write_text("recovered")
    assert provider.resolve() == "recovered"


def test_concurrent_requests_share_a_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sandbox: SandboxPolicy
) -> None:
    resolve = Mock(side_effect=["initial", "replacement"])
    monkeypatch.setattr("omnigent.inner.credential_proxy._resolve_secret", resolve)
    clock = Mock(return_value=0.0)
    provider = RefreshingSecretProvider(
        CredentialSourceSpec(
            kind="file", path=str(tmp_path / "token"), refresh_interval_seconds=60
        ),
        parent_env={},
        sandbox=sandbox,
        clock=clock,
    )
    assert provider.resolve() == "initial"
    clock.return_value = 60.0
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: provider.resolve(), range(16)))
    assert results == ["replacement"] * 16
    assert resolve.call_count == 2


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan"), True, "60"])
def test_parser_rejects_invalid_refresh_interval(interval: object) -> None:
    with pytest.raises(OmnigentError, match="refresh_interval_seconds"):
        _parse_credential_proxy(
            [
                {
                    "type": "gh_basic",
                    "source": {"file": "/private/token", "refresh_interval_seconds": interval},
                }
            ]
        )


@pytest.mark.parametrize("source", [{"env": "TOKEN"}, {"command": "token-broker"}])
def test_environment_and_shell_sources_cannot_refresh(source: dict[str, object]) -> None:
    with pytest.raises(OmnigentError, match="requires a file or unix_socket"):
        _parse_credential_proxy(
            [{"type": "gh_basic", "source": {**source, "refresh_interval_seconds": 60}}]
        )


def test_host_bindings_share_one_provider_per_declaration(
    tmp_path: Path, sandbox: SandboxPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Mock(return_value=0.0)
    resolver = Mock(side_effect=["first", "second", "renewed-first", "renewed-second"])
    monkeypatch.setattr("omnigent.inner.credential_proxy._resolve_secret", resolver)
    monkeypatch.setattr(
        "omnigent.inner.credential_proxy.RefreshingSecretProvider",
        partial(RefreshingSecretProvider, clock=clock),
    )
    source = {"file": str(tmp_path / "token"), "refresh_interval_seconds": 60}
    spec = _parse_credential_proxy(
        [
            {"type": "gh_basic", "source": source},
            {
                "type": "gh_basic",
                "targets": ["git.example.com", "api.example.com"],
                "source": source,
            },
        ]
    )
    runtime = prepare_credential_proxy_runtime(spec, parent_env={}, sandbox=sandbox)
    assert resolver.call_count == 2
    assert [rule.resolve_secret() for rule in runtime.rewrites] == [
        "first",
        "first",
        "second",
        "second",
    ]
    clock.return_value = 60.0
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda rule: rule.resolve_secret(), runtime.rewrites * 8))
    batch = results[:4]
    assert set(batch) == {"renewed-first", "renewed-second"}
    assert batch[0] == batch[1]
    assert batch[2] == batch[3]
    assert results == batch * 8
    assert resolver.call_count == 4


@pytest.mark.parametrize("kind", ["file", "unix_socket"])
@pytest.mark.parametrize(
    "unsafe",
    ["direct", "symlink-out", "symlink-in", "parent-link", "write-file", "hardlink", "relative"],
)
def test_rejects_sandbox_writable_refresh_sources(
    tmp_path: Path, sandbox: SandboxPolicy, kind: str, unsafe: str
) -> None:
    workspace = sandbox.write_roots[0]
    private = tmp_path / "private"
    private.mkdir()
    token = private / "token"
    token.write_text("secret")
    candidate = token
    if unsafe == "direct":
        candidate = workspace / "token"
    elif unsafe == "symlink-out":
        candidate = workspace / "token"
        candidate.symlink_to(token)
    elif unsafe == "symlink-in":
        candidate = private / "link"
        candidate.symlink_to(workspace / "token")
    elif unsafe == "parent-link":
        (private / "link").symlink_to(workspace, target_is_directory=True)
        candidate = private / "link" / "token"
    elif unsafe == "write-file":
        sandbox.write_files.append(token)
    elif unsafe == "hardlink":
        (workspace / "token").hardlink_to(token)
    else:
        candidate = Path("relative-token")
    spec = CredentialSourceSpec(kind=kind, path=str(candidate), refresh_interval_seconds=60)
    with pytest.raises(
        ValueError, match=r"sandbox-writable|hard-linked|absolute|not a Unix socket"
    ):
        RefreshingSecretProvider(spec, parent_env={}, sandbox=sandbox).resolve()


def test_refresh_requires_a_policy_and_revalidates_rotation(
    tmp_path: Path, sandbox: SandboxPolicy
) -> None:
    token = tmp_path / "token"
    token.write_text("initial")
    source = CredentialSourceSpec(kind="file", path=str(token), refresh_interval_seconds=60)
    with pytest.raises(ValueError, match="active sandbox policy"):
        RefreshingSecretProvider(source, parent_env={})
    clock = Mock(return_value=0.0)
    provider = RefreshingSecretProvider(source, parent_env={}, sandbox=sandbox, clock=clock)
    assert provider.resolve() == "initial"
    token.unlink()
    token.symlink_to(sandbox.write_roots[0] / "replacement")
    clock.return_value = 60.0
    with pytest.raises(ValueError, match="sandbox-writable"):
        provider.resolve()


@pytest.mark.parametrize(
    "status,payload",
    [
        (200, b"replacement"),
        (500, b"secret-error"),
        (302, b"redirect"),
        (200, b""),
        (200, b"token\nheader"),
        (200, b"x" * 65537),
    ],
    ids=["rotated", "error", "redirect", "empty", "multiline", "oversize"],
)
def test_private_socket_renewal_and_failed_refresh(
    broker: tuple[Path, dict[str, object]], sandbox: SandboxPolicy, status: int, payload: bytes
) -> None:
    path, state = broker
    clock = Mock(return_value=0.0)
    provider = RefreshingSecretProvider(
        CredentialSourceSpec(kind="unix_socket", path=str(path), refresh_interval_seconds=60),
        parent_env={},
        sandbox=sandbox,
        clock=clock,
    )
    assert provider.resolve() == "initial"
    state.update(status=status, body=payload)
    clock.return_value = 60.0
    if payload == b"replacement":
        assert provider.resolve() == "replacement"
    else:
        with pytest.raises(ValueError):
            provider.resolve()
        state.update(status=200, body=b"recovered")
        assert provider.resolve() == "recovered"


def test_refresh_remains_opt_in(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("initial")
    spec = _parse_credential_proxy([{"type": "gh_basic", "source": {"file": str(token_file)}}])
    runtime = prepare_credential_proxy_runtime(spec, parent_env={})
    token_file.write_text("replacement")
    assert {rule.resolve_secret() for rule in runtime.rewrites} == {"initial"}


@pytest.mark.parametrize("mode", ["direct", "source-alias", "root-alias", "readonly-cwd"])
def test_rejects_sandbox_readable_refresh_sources(
    tmp_path: Path, sandbox: SandboxPolicy, mode: str
) -> None:
    readable = tmp_path / "readable"
    readable.mkdir()
    token = readable / "token"
    token.write_text("secret")
    candidate = token
    cwd = sandbox.write_roots[0]
    if mode == "source-alias":
        candidate = tmp_path / "alias"
        candidate.symlink_to(token)
    elif mode == "root-alias":
        alias = tmp_path / "alias"
        alias.symlink_to(readable, target_is_directory=True)
        sandbox.read_roots = [alias]
    elif mode == "readonly-cwd":
        cwd = readable
    if mode not in ("root-alias", "readonly-cwd"):
        sandbox.read_roots = [readable]
    with pytest.raises(ValueError, match="sandbox-readable"):
        RefreshingSecretProvider(
            CredentialSourceSpec(kind="file", path=str(candidate), refresh_interval_seconds=60),
            parent_env={},
            sandbox=sandbox,
            cwd=cwd,
        )


@pytest.mark.parametrize("refresh", [None, 60])
def test_readonly_broker_is_rejected_before_contact(
    broker: tuple[Path, dict[str, object]], sandbox: SandboxPolicy, refresh: int | None
) -> None:
    path, state = broker
    sandbox.read_roots = [path.parent]
    source = {"unix_socket": str(path), "refresh_interval_seconds": refresh}
    spec = _parse_credential_proxy([{"type": "gh_basic", "source": source}])
    with pytest.raises(ValueError, match="sandbox-readable"):
        prepare_credential_proxy_runtime(spec, parent_env={}, sandbox=sandbox)
    assert state["calls"] == 0


def test_hardlinked_broker_is_rejected_before_contact(
    broker: tuple[Path, dict[str, object]], sandbox: SandboxPolicy
) -> None:
    path, state = broker
    alias = path.parent / "broker-alias.sock"
    alias.hardlink_to(path)
    spec = _parse_credential_proxy([{"type": "gh_basic", "source": {"unix_socket": str(path)}}])
    with pytest.raises(ValueError, match="hard-linked"):
        prepare_credential_proxy_runtime(spec, parent_env={}, sandbox=sandbox)
    assert state["calls"] == 0


def test_refresh_rejects_changed_private_symlink_target(
    tmp_path: Path, sandbox: SandboxPolicy
) -> None:
    original = tmp_path / "original"
    original.write_text("initial")
    target = tmp_path / "replacement"
    target.write_text("replacement")
    alias = tmp_path / "alias"
    alias.symlink_to(original)
    clock = Mock(return_value=0.0)
    provider = RefreshingSecretProvider(
        CredentialSourceSpec(kind="file", path=str(alias), refresh_interval_seconds=60),
        parent_env={},
        sandbox=sandbox,
        clock=clock,
    )
    assert provider.resolve() == "initial"
    assert sandbox.credential_source_paths == [original.resolve()]
    alias.unlink()
    alias.symlink_to(target)
    clock.return_value = 60.0
    with pytest.raises(ValueError, match="symlink target changed"):
        provider.resolve()


@pytest.mark.parametrize(
    "source",
    [
        CredentialSourceSpec(kind="env", env="TOKEN", refresh_interval_seconds=60),
        CredentialSourceSpec(kind="command", command="echo secret", refresh_interval_seconds=60),
    ],
)
def test_runtime_rejects_nonrenewable_sources(
    source: CredentialSourceSpec, sandbox: SandboxPolicy
) -> None:
    spec = CredentialProxySpec(
        entries=[
            CredentialProxyEntry(host="example.com", scheme="bearer", source=source),
        ]
    )
    with pytest.raises(ValueError, match="requires a file or unix_socket"):
        prepare_credential_proxy_runtime(spec, parent_env={"TOKEN": "secret"}, sandbox=sandbox)


@pytest.mark.parametrize("refresh", [None, 60])
def test_missing_broker_fails_at_startup(
    tmp_path: Path, sandbox: SandboxPolicy, refresh: int | None
) -> None:
    spec = _parse_credential_proxy(
        [
            {
                "type": "gh_basic",
                "source": {
                    "unix_socket": str(tmp_path / "missing.sock"),
                    "refresh_interval_seconds": refresh,
                },
            }
        ]
    )
    with pytest.raises(ValueError, match="credential broker is unavailable"):
        prepare_credential_proxy_runtime(spec, parent_env={}, sandbox=sandbox)


def test_socket_without_interval_remains_startup_only(
    broker: tuple[Path, dict[str, object]], sandbox: SandboxPolicy
) -> None:
    path, state = broker
    spec = _parse_credential_proxy([{"type": "gh_basic", "source": {"unix_socket": str(path)}}])
    runtime = prepare_credential_proxy_runtime(spec, parent_env={}, sandbox=sandbox)
    calls = state["calls"]
    state["body"] = b"replacement"
    assert {rule.resolve_secret() for rule in runtime.rewrites} == {"initial"}
    assert state["calls"] == calls
    assert sandbox.credential_source_paths == [path.resolve()]


@pytest.mark.parametrize(
    "kind,refresh", [("file", 60), ("unix_socket", 60), ("unix_socket", None)]
)
def test_native_sandbox_resolution_protects_sources_before_proxy_startup(
    tmp_path: Path,
    sandbox: SandboxPolicy,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    refresh: int | None,
) -> None:
    path = tmp_path / "private-source"
    sandbox.credential_proxy = _parse_credential_proxy(
        [
            {
                "type": "gh_basic",
                "source": {kind: str(path), "refresh_interval_seconds": refresh},
            }
        ]
    )
    backend = Mock(resolve=Mock(return_value=sandbox))
    monkeypatch.setattr("omnigent.inner.sandbox._get_backend", lambda name: backend)
    spec = OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap"))
    policy = resolve_sandbox(spec, sandbox.write_roots[0])
    assert policy.credential_source_paths == [path.resolve()]
    sandbox.read_roots = [path.parent]
    with pytest.raises(ValueError, match="sandbox-readable"):
        resolve_sandbox(spec, sandbox.write_roots[0])


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_socket_deadline_bounds_dribbling_response(
    short_tmp_parent: Path, sandbox: SandboxPolicy, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    stopped = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.recv(4096)
            headers = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n"
            try:
                if phase == "body":
                    self.request.sendall(headers)
                payload = b"x" * 100 if phase == "body" else headers
                for byte in payload:
                    if stopped.wait(0.03):
                        return
                    self.request.sendall(bytes([byte]))
            except OSError:
                pass

    path = short_tmp_parent / "slow.sock"
    monkeypatch.setattr("omnigent.inner.credential_proxy._BROKER_SOURCE_TIMEOUT_SECONDS", 0.3)
    with socketserver.UnixStreamServer(str(path), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            started = time.monotonic()
            provider = RefreshingSecretProvider(
                CredentialSourceSpec(
                    kind="unix_socket", path=str(path), refresh_interval_seconds=60
                ),
                parent_env={},
                sandbox=sandbox,
            )
            with pytest.raises(ValueError, match="credential broker is unavailable"):
                provider.resolve()
            assert time.monotonic() - started < 1.5
        finally:
            stopped.set()
            server.shutdown()
            thread.join()


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS sandbox-exec")
@pytest.mark.parametrize("kind", ["file", "unix_socket"])
def test_seatbelt_denies_sources_despite_implicit_read_grant(
    broker: tuple[Path, dict[str, object]], sandbox: SandboxPolicy, kind: str
) -> None:
    from omnigent.inner.seatbelt_sandbox import _build_profile

    path, _ = broker
    path = path.resolve()
    sandbox.backend_type = "darwin_seatbelt"
    sandbox.allow_network = True
    command = [
        "/usr/bin/curl",
        "--silent",
        "--max-time",
        "2",
        "--unix-socket",
        str(path),
        "http://localhost/token",
    ]
    if kind == "file":
        path = path.parent / "private-token"
        path.write_text("initial")
        command = ["/bin/cat", str(path)]
    spec = _parse_credential_proxy(
        [
            {"type": "gh_basic", "source": {kind: str(path), "refresh_interval_seconds": 60}},
        ]
    )
    prepare_credential_proxy_runtime(spec, parent_env={}, sandbox=sandbox)
    for protected in (False, True):
        policy = sandbox if protected else replace(sandbox, credential_source_paths=None)
        profile = _build_profile(policy, sandbox.write_roots[0], extra_read_paths=[path.parent])
        result = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, *command],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if protected:
            assert result.returncode != 0
            assert "initial" not in result.stdout
        else:
            assert result.returncode == 0, result.stderr
            assert result.stdout == "initial"
