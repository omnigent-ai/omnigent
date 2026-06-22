"""Tests for egress_rules parser validation in omnigent.spec.parser."""

from __future__ import annotations

import pytest

from omnigent.spec.parser import _parse_egress_rules, _parse_os_env_sandbox

# ------------------------------------------------------------------
# _parse_egress_rules — unit tests
# ------------------------------------------------------------------


def test_parse_egress_rules_none() -> None:
    """None input returns None (no egress filtering)."""
    assert _parse_egress_rules(None) is None


def test_parse_egress_rules_empty_list() -> None:
    """Empty list returns None (treated as no filtering)."""
    assert _parse_egress_rules([]) is None


def test_parse_egress_rules_valid() -> None:
    """Valid rules are returned as-is after validation."""
    rules = ["GET api.github.com/repos/**", "* pypi.org/**"]
    result = _parse_egress_rules(rules)
    assert result == rules


def test_parse_egress_rules_not_a_list() -> None:
    """Non-list input raises OmnigentError."""
    from omnigent.errors import OmnigentError

    with pytest.raises(OmnigentError, match="must be a list"):
        _parse_egress_rules("GET api.github.com/**")


def test_parse_egress_rules_non_string_entry() -> None:
    """Non-string entry raises OmnigentError."""
    from omnigent.errors import OmnigentError

    with pytest.raises(OmnigentError, match="must be strings"):
        _parse_egress_rules([123])


def test_parse_egress_rules_invalid_syntax() -> None:
    """Invalid rule syntax raises OmnigentError."""
    from omnigent.errors import OmnigentError

    with pytest.raises(OmnigentError, match="is invalid"):
        _parse_egress_rules(["BADMETHOD api.github.com/**"])


# ------------------------------------------------------------------
# _parse_os_env_sandbox — egress_rules validation
# ------------------------------------------------------------------


def test_sandbox_egress_rules_rejected_for_non_filtering_backends() -> None:
    """
    Egress rules require a backend that can hard-enforce network
    isolation at spawn time. The ``none`` backend (sandbox disabled)
    must reject ``egress_rules`` with a fail-loud error naming the
    backends that do work.

    The error message references both ``linux_bwrap`` AND
    ``darwin_seatbelt`` so a macOS spec author sees the correct
    option without having to scan platform docs.
    """
    from omnigent.errors import OmnigentError

    raw = {
        "type": "none",
        "egress_rules": ["GET api.github.com/**"],
    }
    with pytest.raises(OmnigentError, match=r"linux_bwrap.*darwin_seatbelt") as excinfo:
        _parse_os_env_sandbox(raw)
    # Both backend names must appear in the error so the user knows
    # which spec change unblocks them.
    msg = str(excinfo.value)
    assert "linux_bwrap" in msg
    assert "darwin_seatbelt" in msg


@pytest.mark.parametrize(
    "backend_type",
    ["linux_bwrap", "darwin_seatbelt"],
    ids=["bwrap", "seatbelt"],
)
def test_sandbox_egress_rules_accepted_for_hard_enforcing_backends(
    backend_type: str,
) -> None:
    """
    ``egress_rules`` is accepted for both ``linux_bwrap`` (Linux)
    AND ``darwin_seatbelt`` (macOS). Parser-level validation must
    not be platform-gated — a Linux YAML edited on macOS (or vice
    versa) must still parse, with platform-specific errors raised
    later at resolve/spawn time.
    """
    raw = {
        "type": backend_type,
        "egress_rules": ["GET api.github.com/**", "* pypi.org/**"],
    }
    spec = _parse_os_env_sandbox(raw)
    assert spec is not None
    assert spec.egress_rules == ["GET api.github.com/**", "* pypi.org/**"]


def test_sandbox_egress_rules_none_with_any_backend() -> None:
    """No egress_rules is valid with any backend type."""
    for backend in ("linux_bwrap", "darwin_seatbelt", "none"):
        raw = {"type": backend}
        spec = _parse_os_env_sandbox(raw)
        assert spec is not None
        assert spec.egress_rules is None


def test_s2_sandbox_egress_allow_private_destinations_defaults_to_false() -> None:
    """
    S2: when ``egress_allow_private_destinations`` is omitted from
    the YAML, the spec MUST default to ``False`` — i.e. blocking
    private/loopback destinations is on by default. Flipping this
    default silently would re-introduce the DNS-rebinding
    vulnerability against agents with permissive wildcard rules.
    """
    for backend in ("linux_bwrap", "darwin_seatbelt", "none"):
        raw = {"type": backend}
        spec = _parse_os_env_sandbox(raw)
        assert spec is not None
        assert spec.egress_allow_private_destinations is False, (
            f"backend={backend!r}: default for "
            "egress_allow_private_destinations must be False (block "
            "private destinations). Changing the default silently is "
            "an S2 regression."
        )


def test_s2_sandbox_egress_allow_private_destinations_accepts_true() -> None:
    """
    S2: an explicit ``egress_allow_private_destinations: true`` in
    the YAML opts an agent out of the private-destination block.
    This is the auditable escape hatch for agents that legitimately
    need to reach intranet services.
    """
    raw = {
        "type": "linux_bwrap",
        "egress_rules": ["GET intranet.example/**"],
        "egress_allow_private_destinations": True,
    }
    spec = _parse_os_env_sandbox(raw)
    assert spec is not None
    assert spec.egress_allow_private_destinations is True


def test_s2_sandbox_egress_allow_private_destinations_rejects_non_bool() -> None:
    """
    S2: a non-boolean value for ``egress_allow_private_destinations``
    is rejected at parse time. Strings like ``"true"`` / ``"yes"`` are
    a common YAML footgun — accepting them would silently grant
    private-destination access on a typo.
    """
    import pytest

    from omnigent.errors import OmnigentError

    raw = {
        "type": "linux_bwrap",
        "egress_allow_private_destinations": "true",
    }
    with pytest.raises(OmnigentError, match=r"must be a boolean"):
        _parse_os_env_sandbox(raw)
