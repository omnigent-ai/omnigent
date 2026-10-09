"""``omnigent sandbox create --provider openshell`` must ship the wheel bundle.

An OpenShell gateway rejects any single inbound gRPC message over 1 MiB, so the
~13 MB wheel tarball has to reach it in ``ExecSandbox`` messages under that cap.
The gateway here is a local stand-in with the same limit (see ``_FakeGateway``);
the real ``omnigent`` CLI runs under a PTY, so no Docker or gateway is needed::

    pytest tests/e2e/test_openshell_sandbox_create_upload_e2e.py -v
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import uuid
from concurrent import futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from omnigent.onboarding.sandboxes.bootstrap import DEFAULT_WHEELS_TGZ

pexpect = pytest.importorskip("pexpect")
grpc = pytest.importorskip("grpc")
pytest.importorskip("openshell", reason="openshell SDK not installed (omnigent[openshell])")

from openshell._proto import (  # noqa: E402
    datamodel_pb2,
    openshell_pb2,
    openshell_pb2_grpc,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKSPACE = "default"
_GATEWAY_NAME = "local"
_GATEWAY_MAX_MESSAGE_BYTES = 1 << 20
_REMOTE_WHEELS_TGZ = "/tmp/oa-wheels.tgz"
_CREATE_TIMEOUT_S = 480


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class _FakeGateway(openshell_pb2_grpc.OpenShellServicer):
    """OpenShell gateway stand-in: sandbox CRUD plus exec against a scratch root."""

    sandbox_root: Path
    exec_requests: list[openshell_pb2.ExecSandboxRequest] = field(default_factory=list)
    rejected_message_sizes: list[int] = field(default_factory=list)
    _sandboxes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        home = self.sandbox_root / "sandbox"
        home.mkdir(parents=True)
        (self.sandbox_root / "tmp").mkdir()
        stub_bin = self.sandbox_root / "bin"
        stub_bin.mkdir()
        # The host image's pip would install the shipped wheels into its venv;
        # here that must not touch the machine running the test.
        pip_stub = stub_bin / "pip"
        pip_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        pip_stub.chmod(0o755)
        # Login shells (`bash -lc`) re-derive PATH from /etc/profile; restore the stub.
        (home / ".bash_profile").write_text(f'export PATH="{stub_bin}:$PATH"\n', encoding="utf-8")

    def _sandbox(self, name: str, sandbox_id: str) -> openshell_pb2.Sandbox:
        return openshell_pb2.Sandbox(
            metadata=datamodel_pb2.ObjectMeta(id=sandbox_id, name=name, workspace=_WORKSPACE),
            status=openshell_pb2.SandboxStatus(phase=openshell_pb2.SANDBOX_PHASE_READY),
        )

    def CreateSandbox(
        self, request: openshell_pb2.CreateSandboxRequest, context: grpc.ServicerContext
    ) -> openshell_pb2.SandboxResponse:
        name = request.name or f"eager-otter-{len(self._sandboxes) + 1}"
        sandbox_id = f"sb-{uuid.uuid4().hex[:12]}"
        self._sandboxes[name] = sandbox_id
        return openshell_pb2.SandboxResponse(sandbox=self._sandbox(name, sandbox_id))

    def GetSandbox(
        self, request: openshell_pb2.GetSandboxRequest, context: grpc.ServicerContext
    ) -> openshell_pb2.SandboxResponse:
        sandbox_id = self._sandboxes.get(request.name)
        if sandbox_id is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"sandbox {request.name!r} not found")
        return openshell_pb2.SandboxResponse(sandbox=self._sandbox(request.name, sandbox_id))

    def DeleteSandbox(
        self, request: openshell_pb2.DeleteSandboxRequest, context: grpc.ServicerContext
    ) -> openshell_pb2.DeleteSandboxResponse:
        return openshell_pb2.DeleteSandboxResponse(
            deleted=self._sandboxes.pop(request.name, None) is not None
        )

    def ExecSandbox(
        self, request: openshell_pb2.ExecSandboxRequest, context: grpc.ServicerContext
    ) -> Iterator[openshell_pb2.ExecSandboxEvent]:
        size = request.ByteSize()
        if size > _GATEWAY_MAX_MESSAGE_BYTES:
            self.rejected_message_sizes.append(size)
            context.abort(
                grpc.StatusCode.OUT_OF_RANGE,
                "Error, decoded message length too large: "
                f"found {size} bytes, the limit is: {_GATEWAY_MAX_MESSAGE_BYTES} bytes",
            )
        self.exec_requests.append(request)
        result = self._run(request)
        if result.stdout:
            yield openshell_pb2.ExecSandboxEvent(
                stdout=openshell_pb2.ExecSandboxStdout(data=result.stdout)
            )
        if result.stderr:
            yield openshell_pb2.ExecSandboxEvent(
                stderr=openshell_pb2.ExecSandboxStderr(data=result.stderr)
            )
        yield openshell_pb2.ExecSandboxEvent(
            exit=openshell_pb2.ExecSandboxExit(exit_code=result.returncode)
        )

    def _localize(self, text: str) -> str:
        """Point absolute ``/tmp`` and ``/sandbox`` paths at the scratch root."""
        return re.sub(
            r"(?<![\w/])/(tmp|sandbox)(?=/|\b)",
            lambda match: str(self.sandbox_root / match.group(1)),
            text,
        )

    def _run(
        self, request: openshell_pb2.ExecSandboxRequest
    ) -> subprocess.CompletedProcess[bytes]:
        env = os.environ.copy()
        env.update({key: self._localize(value) for key, value in request.environment.items()})
        env["PATH"] = f"{self.sandbox_root / 'bin'}:{env.get('PATH', '')}"
        return subprocess.run(
            [self._localize(arg) for arg in request.command],
            input=bytes(request.stdin),
            cwd=self._localize(request.workdir) if request.workdir else self.sandbox_root,
            env=env,
            capture_output=True,
            timeout=request.timeout_seconds or None,
            check=False,
        )


@dataclass
class _RunningGateway:
    servicer: _FakeGateway
    port: int


@pytest.fixture
def fake_gateway(tmp_path: Path) -> Iterator[_RunningGateway]:
    servicer = _FakeGateway(sandbox_root=tmp_path / "sandbox-fs")
    # Unlimited receive size so the servicer, not grpc-python, applies the
    # gateway's cap and reports it the way the real gateway does.
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[("grpc.max_receive_message_length", -1)],
    )
    openshell_pb2_grpc.add_OpenShellServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield _RunningGateway(servicer=servicer, port=port)
    finally:
        server.stop(grace=None)


@pytest.fixture
def openshell_config_home(tmp_path: Path, fake_gateway: _RunningGateway) -> Path:
    """The on-disk state ``openshell gateway select local`` leaves behind."""
    config_home = tmp_path / "xdg-config"
    gateway_dir = config_home / "openshell" / "gateways" / _GATEWAY_NAME
    gateway_dir.mkdir(parents=True)
    (gateway_dir / "metadata.json").write_text(
        json.dumps(
            {
                "name": _GATEWAY_NAME,
                "gateway_endpoint": f"http://127.0.0.1:{fake_gateway.port}",
                "auth_mode": "none",
            }
        ),
        encoding="utf-8",
    )
    (config_home / "openshell" / "active_gateway").write_text(
        f"{_GATEWAY_NAME}\n", encoding="utf-8"
    )
    return config_home


def _run_sandbox_create(config_home: Path) -> tuple[int | None, str]:
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(config_home)
    env.pop("OPENSHELL_GATEWAY", None)
    env["PYTHONPATH"] = os.pathsep.join([str(_REPO_ROOT), *filter(None, [env.get("PYTHONPATH")])])
    env["PYTHONUNBUFFERED"] = "1"
    child = pexpect.spawn(
        sys.executable,
        [
            "-m",
            "omnigent",
            "sandbox",
            "create",
            "--provider",
            "openshell",
            "--server",
            f"http://127.0.0.1:{_free_port()}",
        ],
        cwd=str(_REPO_ROOT),
        env=env,
        encoding="utf-8",
        codec_errors="replace",
        timeout=_CREATE_TIMEOUT_S,
        dimensions=(50, 200),
    )
    try:
        child.expect(pexpect.EOF)
        output = child.before or ""
    finally:
        child.close()
    return child.exitstatus, output


@pytest.mark.timeout(_CREATE_TIMEOUT_S + 60)
def test_sandbox_create_ships_wheel_bundle_within_gateway_message_cap(
    fake_gateway: _RunningGateway, openshell_config_home: Path
) -> None:
    exit_status, output = _run_sandbox_create(openshell_config_home)
    gateway = fake_gateway.servicer

    assert not gateway.rejected_message_sizes, (
        "the OpenShell gateway rejected the wheel upload: `sandbox create` sent "
        f"ExecSandbox message(s) of {gateway.rejected_message_sizes} bytes, over the "
        f"gateway's {_GATEWAY_MAX_MESSAGE_BYTES}-byte cap, so creation aborted after "
        f"the wheels were built. CLI output:\n{output}"
    )
    assert exit_status == 0, f"`sandbox create` exited {exit_status}. CLI output:\n{output}"

    uploaded = gateway.sandbox_root / _REMOTE_WHEELS_TGZ.lstrip("/")
    assert uploaded.is_file(), f"wheel bundle never landed at {uploaded}. CLI output:\n{output}"
    assert uploaded.read_bytes() == Path(DEFAULT_WHEELS_TGZ).read_bytes(), (
        "wheel bundle in the sandbox differs from the locally built tarball"
    )
    assert "Sandbox ready." in output, f"missing ready banner. CLI output:\n{output}"
