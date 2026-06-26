"""Loader reuses the canonical credential_broker parser (plan Task 4)."""

import pytest

from omnigent.inner.loader import load_agent_def


def test_loader_roundtrips_broker():
    agent = load_agent_def(
        {
            "name": "t",
            "os_env": {
                "type": "caller_process",
                "sandbox": {
                    "type": "linux_bwrap",
                    "allow_network": True,
                    "credential_broker": {
                        "load": [{"from": "env", "names": ["PGHOST"]}],
                        "groups": {"pg": [{"env": "PGPASSWORD", "optional": True}]},
                        "tools": {"psql": {"credentials": ["pg"]}},
                    },
                },
            },
        }
    )
    broker = agent.os_env.sandbox.credential_broker
    assert broker is not None
    assert broker.tools["psql"].credentials == ["pg"]
    assert broker.groups["pg"].fields[0].env == "PGPASSWORD"
    assert broker.load[0].from_ == "env"


def test_loader_validates_broker_egress_conflict():
    with pytest.raises(ValueError, match="not compatible with egress_rules"):
        load_agent_def(
            {
                "name": "t",
                "os_env": {
                    "type": "caller_process",
                    "sandbox": {
                        "type": "linux_bwrap",
                        "egress_rules": ["GET api.x/**"],
                        "credential_broker": {
                            "groups": {"pg": [{"env": "PGHOST"}]},
                            "tools": {"psql": {"credentials": ["pg"]}},
                        },
                    },
                },
            }
        )
