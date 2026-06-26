"""Datamodel tests for the non-HTTP credential broker (plan Task 1)."""

from omnigent.inner.datamodel import (
    CredentialBrokerField,
    CredentialBrokerGroup,
    CredentialBrokerLoadSource,
    CredentialBrokerSpec,
    CredentialBrokerTool,
    CredentialSourceSpec,
    OSEnvSandboxSpec,
)


def test_field_defaults():
    f = CredentialBrokerField(env="PGPASSWORD")
    assert f.key is None
    assert f.optional is False
    assert f.fallback is None


def test_spec_composition():
    spec = CredentialBrokerSpec(
        load=[CredentialBrokerLoadSource(from_="env", names=["PGHOST"])],
        groups={
            "postgres": CredentialBrokerGroup(
                fields=[
                    CredentialBrokerField(
                        env="PGPASSWORD",
                        optional=True,
                        fallback=CredentialSourceSpec(kind="command", command="echo x"),
                    )
                ]
            )
        },
        tools={"psql": CredentialBrokerTool(credentials=["postgres"])},
    )
    assert spec.tools["psql"].credentials == ["postgres"]
    assert spec.groups["postgres"].fields[0].fallback.command == "echo x"
    assert spec.load[0].from_ == "env"


def test_sandbox_spec_has_credential_broker_field():
    sb = OSEnvSandboxSpec(type="linux_bwrap", credential_broker=CredentialBrokerSpec())
    assert isinstance(sb.credential_broker, CredentialBrokerSpec)
    assert OSEnvSandboxSpec(type="linux_bwrap").credential_broker is None
