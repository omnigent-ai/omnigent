from __future__ import annotations

import base64
import http.server
import socketserver
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.errors import OmnigentError
from omnigent.inner.credential_proxy import (
    RefreshingSecretProvider,
    prepare_credential_proxy_runtime,
)
from omnigent.inner.datamodel import CredentialSourceSpec
from omnigent.inner.egress.proxy import EgressProxy
from omnigent.inner.sandbox import SandboxPolicy
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
