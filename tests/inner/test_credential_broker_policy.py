"""SandboxPolicy carries credential_broker parent-side only (plan Task 2)."""

from pathlib import Path

from omnigent.inner.datamodel import CredentialBrokerSpec
from omnigent.inner.sandbox import SandboxPolicy, with_additional_write_roots


def _policy(**kw) -> SandboxPolicy:
    return SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=None,
        write_roots=[Path("/x")],
        write_files=[],
        allow_network=True,
        **kw,
    )


def test_broker_not_serialized_to_helper():
    p = _policy(credential_broker=CredentialBrokerSpec())
    assert "credential_broker" not in p.to_jsonable()


def test_broker_preserved_across_clone():
    spec = CredentialBrokerSpec()
    p = with_additional_write_roots(_policy(credential_broker=spec), [Path("/scratch")])
    assert p.credential_broker is spec
