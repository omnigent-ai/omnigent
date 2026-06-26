"""Parser tests for credential_broker (plan Tasks 3, 3b)."""

import pytest

from omnigent.errors import OmnigentError
from omnigent.spec.parser import _parse_os_env_sandbox


def _broker(**over):
    b = {
        "groups": {"g": [{"env": "PGHOST"}]},
        "tools": {"psql": {"credentials": ["g"]}},
    }
    b.update(over)
    return b


def test_parse_broker_full():
    raw = {
        "type": "linux_bwrap",
        "allow_network": True,
        "credential_broker": {
            "load": [{"from": "env", "names": ["PGHOST"]}],
            "groups": {
                "postgres": [
                    {
                        "env": "PGPASSWORD",
                        "optional": True,
                        "fallback": {"command": "az x -o tsv"},
                    },
                    {"env": "PGHOST"},
                ]
            },
            "tools": {"psql": {"credentials": ["postgres"]}},
        },
    }
    spec = _parse_os_env_sandbox(raw)
    b = spec.credential_broker
    assert b is not None
    assert b.load[0].from_ == "env" and b.load[0].names == ["PGHOST"]
    f0 = b.groups["postgres"].fields[0]
    assert f0.env == "PGPASSWORD" and f0.optional is True and f0.fallback.command == "az x -o tsv"
    assert b.tools["psql"].credentials == ["postgres"]


def test_parse_broker_unknown_group_raises():
    raw = {
        "type": "linux_bwrap",
        "allow_network": True,
        "credential_broker": {"groups": {}, "tools": {"psql": {"credentials": ["nope"]}}},
    }
    with pytest.raises(OmnigentError, match="unknown group"):
        _parse_os_env_sandbox(raw)


def test_parse_broker_interpreter_hook_env_raises():
    raw = {
        "type": "linux_bwrap",
        "allow_network": True,
        "credential_broker": {
            "groups": {"g": [{"env": "LD_PRELOAD"}]},
            "tools": {"psql": {"credentials": ["g"]}},
        },
    }
    with pytest.raises(OmnigentError, match=r"interpreter hook|LD_PRELOAD"):
        _parse_os_env_sandbox(raw)


def test_parse_broker_rejects_egress_rules():
    raw = {
        "type": "linux_bwrap",
        "allow_network": True,
        "egress_rules": ["GET api.x/**"],
        "credential_broker": _broker(),
    }
    with pytest.raises(OmnigentError, match="not compatible with egress_rules"):
        _parse_os_env_sandbox(raw)


def test_parse_broker_rejects_no_network():
    raw = {"type": "linux_bwrap", "allow_network": False, "credential_broker": _broker()}
    with pytest.raises(OmnigentError, match="allow_network"):
        _parse_os_env_sandbox(raw)


def test_parse_broker_rejects_type_none():
    raw = {"type": "none", "credential_broker": _broker()}
    with pytest.raises(OmnigentError, match="linux_bwrap"):
        _parse_os_env_sandbox(raw)


def test_parse_broker_unknown_key_rejected():
    raw = {
        "type": "linux_bwrap",
        "allow_network": True,
        "credential_broker": {
            "groups": {"g": [{"env": "PGHOST", "bogus": 1}]},
            "tools": {"psql": {"credentials": ["g"]}},
        },
    }
    with pytest.raises(OmnigentError):
        _parse_os_env_sandbox(raw)
