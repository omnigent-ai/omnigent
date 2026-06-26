"""Broker core: store loading, resolution, shims, server (plan Tasks 5-8, 10)."""

import json
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from omnigent.inner.credential_broker import (
    _BrokerServer,
    _load_store,
    _resolve_tool_env,
    _write_shims,
    prepare_credential_broker_runtime,
)
from omnigent.inner.datamodel import (
    CredentialBrokerField,
    CredentialBrokerGroup,
    CredentialBrokerLoadSource,
    CredentialBrokerSpec,
    CredentialBrokerTool,
    CredentialSourceSpec,
)


@pytest.fixture
def short_tmp():
    """A short tmp dir so AF_UNIX socket paths stay under the ~104B sun_path
    limit (pytest's nested tmp_path overflows it on macOS)."""
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _spec() -> CredentialBrokerSpec:
    return CredentialBrokerSpec(
        groups={
            "pg": CredentialBrokerGroup(
                fields=[
                    CredentialBrokerField(env="PGHOST"),
                    CredentialBrokerField(
                        env="PGPASSWORD",
                        optional=True,
                        fallback=CredentialSourceSpec(kind="command", command="printf tok"),
                    ),
                    CredentialBrokerField(env="PGOPT", optional=True),
                ]
            )
        },
        tools={"psql": CredentialBrokerTool(credentials=["pg"])},
    )


# --- Task 5: store ---------------------------------------------------------


def test_load_store_file_and_env(tmp_path):
    f = tmp_path / "dev.env"
    f.write_text("PGHOST=db.example\n# comment\n\nPGPORT=5432\n")
    f.chmod(0o600)
    store = _load_store(
        [
            CredentialBrokerLoadSource(from_="file", path=str(f)),
            CredentialBrokerLoadSource(from_="env", names=["PGUSER"]),
        ],
        parent_env={"PGUSER": "alice", "IGNORED": "x"},
    )
    assert store == {"PGHOST": "db.example", "PGPORT": "5432", "PGUSER": "alice"}


# --- Task 6: resolution ----------------------------------------------------


def test_resolve_prefers_store_then_fallback_then_skip():
    out = _resolve_tool_env(_spec(), "psql", {"PGHOST": "h"}, command_env={})
    assert out == {"PGHOST": "h", "PGPASSWORD": "tok"}


def test_resolve_required_missing_raises():
    spec = _spec()
    spec.groups["pg"].fields[1] = CredentialBrokerField(
        env="PGPASSWORD"
    )  # required, no store/fallback
    with pytest.raises(ValueError, match="required field PGPASSWORD"):
        _resolve_tool_env(spec, "psql", {"PGHOST": "h"}, command_env={})


# --- Task 7: shims ---------------------------------------------------------


def test_write_shims(tmp_path):
    d = _write_shims(["psql", "pytest"], tmp_path / "shims", socket_path=tmp_path / "b.sock")
    import os

    psql = d / "psql"
    assert psql.exists() and os.access(psql, os.X_OK)
    assert (d / "cred_broker_client.py").exists()
    body = psql.read_text()
    assert '--tool "psql"' in body and str(tmp_path / "b.sock") in body
    assert "import omnigent" not in (d / "cred_broker_client.py").read_text()


# --- Task 8: server (token, unknown tool, value-free errors) ---------------


def _ask(sock_path, payload):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(str(sock_path))
    c.sendall((json.dumps(payload) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        ch = c.recv(4096)
        if not ch:
            break
        buf += ch
    c.close()
    return json.loads(buf)


def test_server_token_and_unknown_tool(short_tmp):
    srv = _BrokerServer(
        spec=_spec(),
        store={"PGHOST": "h"},
        parent_env={},
        command_env={},
        socket_path=short_tmp / "b.sock",
        auth_token="T",
    )
    srv.start()
    try:
        ok = _ask(srv.socket_path, {"tool": "psql", "token": "T"})
        assert ok["env"]["PGHOST"] == "h" and ok["env"]["PGPASSWORD"] == "tok"
        assert _ask(srv.socket_path, {"tool": "psql", "token": "WRONG"}) == {"error": "denied"}
        assert _ask(srv.socket_path, {"tool": "rm", "token": "T"}) == {"error": "unknown tool"}
        import os

        assert oct(os.stat(srv.socket_path).st_mode)[-3:] == "600"
    finally:
        srv.stop()


# --- Task 10: runtime ------------------------------------------------------


def test_prepare_runtime(short_tmp):
    rt = prepare_credential_broker_runtime(
        _spec(), parent_env={"PATH": "/usr/bin:/bin"}, command_env={}, scratch_dir=short_tmp
    )
    assert rt is not None
    try:
        assert (rt.shim_dir / "psql").exists()
        assert rt.socket_path.exists()
        assert rt.auth_token
    finally:
        rt.stop()
    assert not rt.socket_path.exists()


def test_prepare_runtime_none_spec(tmp_path):
    assert (
        prepare_credential_broker_runtime(
            None, parent_env={}, command_env={}, scratch_dir=tmp_path
        )
        is None
    )
