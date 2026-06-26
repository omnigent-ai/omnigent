"""End-to-end: a sandboxed agent shell uses a brokered tool's cred without the
cred entering the agent's ambient env (plan Task 11).

Skipped unless an active sandbox backend is available (bwrap on Linux, seatbelt
on macOS). The broker socket lives in the helper's own scratch tmpdir (short
path), so this does not hit the AF_UNIX sun_path limit.
"""

import shutil
import sys
from pathlib import Path

import pytest

from omnigent.inner.datamodel import (
    CredentialBrokerField,
    CredentialBrokerGroup,
    CredentialBrokerLoadSource,
    CredentialBrokerSpec,
    CredentialBrokerTool,
    OSEnvSandboxSpec,
    OSEnvSpec,
)
from omnigent.inner.os_env import create_os_environment

# Repo root so the sandbox-spawned helper (cwd is a throwaway tmp dir here) can
# import omnigent via PYTHONPATH. In production omnigent lives in site-packages
# (bound via the python install root); in this editable-install dev tree the
# source is the repo root, so it must be granted explicitly — mirrors
# tests/inner/sandbox/conftest.py:_repo_root_for_pythonpath.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])

pytestmark = pytest.mark.skipif(
    not (sys.platform == "darwin" or shutil.which("bwrap")),
    reason="needs an active sandbox backend (seatbelt/bwrap)",
)


def _backend() -> str:
    return "darwin_seatbelt" if sys.platform == "darwin" else "linux_bwrap"


def _make_spec(work, env_file, fake) -> OSEnvSpec:
    return OSEnvSpec(
        type="caller_process",
        cwd=str(work),
        sandbox=OSEnvSandboxSpec(
            type=_backend(),
            allow_network=True,
            write_paths=["."],
            read_paths=[str(work), _REPO_ROOT],
            credential_broker=CredentialBrokerSpec(
                load=[CredentialBrokerLoadSource(from_="file", path=str(env_file))],
                groups={
                    "pg": CredentialBrokerGroup(fields=[CredentialBrokerField(env="PGPASSWORD")])
                },
                tools={"faketool": CredentialBrokerTool(credentials=["pg"], binary=str(fake))},
            ),
        ),
    )


async def _run(work, env_file, fake):
    env = create_os_environment(_make_spec(work, env_file, fake))
    try:
        via = await env.shell("faketool")
        leak = await env.shell('printf "%s" "${PGPASSWORD:-EMPTY}"')
        return via, leak
    finally:
        env.close()


async def test_broker_cred_reaches_tool_not_agent(tmp_path):
    (tmp_path / "dev.env").write_text("PGPASSWORD=s3cret\n")
    (tmp_path / "dev.env").chmod(0o600)
    work = tmp_path / "work"
    work.mkdir()
    fake = work / "faketool"
    fake.write_text('#!/bin/bash\necho "PG=$PGPASSWORD"\n')
    fake.chmod(0o755)

    via, leak = await _run(work, tmp_path / "dev.env", fake)
    assert "PG=s3cret" in via.get("stdout", ""), via
    assert leak.get("stdout", "").strip() == "EMPTY", leak


async def test_broker_cred_reaches_tool_cwd_outside_repo(tmp_path):
    # Regression guard for the self-contained client: the shim must work when
    # cwd is NOT the omnigent repo (only sys.executable is guaranteed bound).
    (tmp_path / "dev.env").write_text("PGPASSWORD=s3cret\n")
    (tmp_path / "dev.env").chmod(0o600)
    work = tmp_path / "elsewhere"
    work.mkdir()
    fake = work / "faketool"
    fake.write_text('#!/bin/bash\necho "PG=$PGPASSWORD"\n')
    fake.chmod(0o755)

    via, _ = await _run(work, tmp_path / "dev.env", fake)
    assert "PG=s3cret" in via.get("stdout", ""), via
