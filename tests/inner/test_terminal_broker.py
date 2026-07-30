"""Terminal surface wires the credential broker onto PATH (plan Task 12).

The broker mechanism (socket/shim/client) is covered end-to-end by
test_os_env_broker_e2e; here we verify the terminal-specific wiring:
_bootstrap_credential_broker creates the broker, prepends the shim dir to PATH,
delivers the token via env, and registers the scratch dir as a write root.
"""

import pytest

from omnigent.inner.datamodel import (
    CredentialBrokerField,
    CredentialBrokerGroup,
    CredentialBrokerSpec,
    CredentialBrokerTool,
)
from omnigent.inner.sandbox import SandboxPolicy, cleanup_private_tmpdir
from omnigent.inner.terminal import TerminalInstance


def _broker_spec(*, allow_terminal: bool = True) -> CredentialBrokerSpec:
    return CredentialBrokerSpec(
        groups={"pg": CredentialBrokerGroup(fields=[CredentialBrokerField(env="PGHOST")])},
        tools={"psql": CredentialBrokerTool(credentials=["pg"])},
        allow_terminal=allow_terminal,
    )


def _policy(spec: CredentialBrokerSpec) -> SandboxPolicy:
    return SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
        credential_broker=spec,
    )


def test_bootstrap_credential_broker_wires_path_token_and_write_root(tmp_path):
    policy = _policy(_broker_spec())
    term = TerminalInstance(
        name="t",
        session_key="s",
        socket_path=tmp_path / "x.sock",
        private_dir=tmp_path,
        sandbox_policy=policy,
    )
    env = {"PATH": "/usr/bin:/bin"}
    new_policy = term._bootstrap_credential_broker(policy, env)
    try:
        rt = term._broker_runtime
        assert rt is not None
        assert env["PATH"].startswith(f"{rt.shim_dir}/") or env["PATH"].startswith(
            str(rt.shim_dir)
        )
        assert env["OMNIGENT_CRED_BROKER_TOKEN"] == rt.auth_token
        assert (rt.shim_dir / "psql").exists()
        assert (rt.shim_dir / "cred_broker_client.py").exists()
        resolved_roots = [r.resolve() for r in new_policy.write_roots]
        assert term._broker_tmpdir.resolve() in resolved_roots
    finally:
        if term._broker_runtime is not None:
            term._broker_runtime.stop()
        cleanup_private_tmpdir(term._broker_tmpdir)


def test_bootstrap_credential_broker_refused_without_allow_terminal(tmp_path):
    # The terminal path leaks the token via process env, so it refuses to start
    # the broker unless the deployment explicitly opts into uid-only isolation.
    policy = _policy(_broker_spec(allow_terminal=False))
    term = TerminalInstance(
        name="t",
        session_key="s",
        socket_path=tmp_path / "x.sock",
        private_dir=tmp_path,
        sandbox_policy=policy,
    )
    with pytest.raises(RuntimeError, match="allow_terminal"):
        term._bootstrap_credential_broker(policy, {"PATH": "/usr/bin:/bin"})
    assert term._broker_runtime is None
