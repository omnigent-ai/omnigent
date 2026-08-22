"""Tests for the locally cached "no policy can fire" gate.

The gate suppresses a *blocking* server round trip on every native-harness
tool call, so these tests care as much about when it refuses to skip as about
when it does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.native_policy_gate import (
    GATE_FILE,
    GATE_TTL_S,
    PolicyGate,
    clear_gate,
    clear_gate_for_session,
    forget_session,
    may_skip_policy_call,
    note_session_bridge_dir,
    read_gate,
    record_gate,
)

_AFFIRMATIVE = {"no_policies": True}


def test_recorded_gate_licenses_skipping_until_it_expires(tmp_path: Path) -> None:
    """
    An affirmative gate skips the server, and stops once it expires.

    The skip is what removes two blocking round trips per tool call; the
    expiry is what bounds a missed invalidation. Fails if a recorded gate
    doesn't suppress the call, or if it suppresses it forever — which would
    leave a session that gained a policy ungoverned indefinitely.
    """
    record_gate(tmp_path, _AFFIRMATIVE, ttl_s=10.0)
    gate = read_gate(tmp_path)
    assert gate is not None
    assert gate.no_policies is True
    assert may_skip_policy_call(tmp_path, now=gate.expires_at - 1) is True
    assert may_skip_policy_call(tmp_path, now=gate.expires_at + 1) is False


def test_a_response_without_a_gate_clears_the_record(tmp_path: Path) -> None:
    """
    Absence of the field revokes any earlier gate.

    The server reports the posture only while nothing can fire, so a
    response that stops carrying it means policies now exist. Fails if a
    stale affirmative record survives — the session would keep skipping its
    own enforcement for the rest of the TTL.
    """
    record_gate(tmp_path, _AFFIRMATIVE)
    assert may_skip_policy_call(tmp_path) is True
    record_gate(tmp_path, None)
    assert read_gate(tmp_path) is None
    assert may_skip_policy_call(tmp_path) is False


def test_a_negative_gate_never_licenses_skipping(tmp_path: Path) -> None:
    """
    Only ``no_policies: true`` counts; anything else is "ask the server".

    Fails if a truthy-looking payload (``"true"``, ``1``, an empty dict) is
    accepted as permission to skip enforcement.
    """
    for payload in ({"no_policies": False}, {"no_policies": "true"}, {"no_policies": 1}, {}, 7):
        record_gate(tmp_path, payload)
        assert may_skip_policy_call(tmp_path) is False, payload


def test_sys_add_policy_always_asks_the_server(tmp_path: Path) -> None:
    """
    The one tool the engine gates unconditionally is never skipped.

    ``sys_add_policy`` runs behind an injected ASK so an agent cannot
    install policies on itself unseen; the server's own fast path exempts
    it, and so must this one. Fails if a gate lets that call through
    silently.
    """
    record_gate(tmp_path, _AFFIRMATIVE)
    assert may_skip_policy_call(tmp_path, tool_name="Bash") is True
    assert may_skip_policy_call(tmp_path, tool_name="sys_add_policy") is False
    assert may_skip_policy_call(tmp_path, tool_name="mcp__omnigent__sys_add_policy") is False


def test_env_opt_out_sends_every_hook_to_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``OMNIGENT_NATIVE_POLICY_GATE=0`` disables skipping outright.

    The escape hatch for an operator who wants every hook evaluated
    server-side. Fails if the flag is ignored, leaving no way to turn the
    optimization off.
    """
    record_gate(tmp_path, _AFFIRMATIVE)
    for value in ("0", "false", "no", "off", "FALSE"):
        monkeypatch.setenv("OMNIGENT_NATIVE_POLICY_GATE", value)
        assert may_skip_policy_call(tmp_path) is False, value
    monkeypatch.setenv("OMNIGENT_NATIVE_POLICY_GATE", "1")
    assert may_skip_policy_call(tmp_path) is True


@pytest.mark.parametrize(
    "raw",
    ["", "not json", "[]", '{"no_policies": true}', '{"expires_at": 1}', '{"no_policies": "x"}'],
)
def test_an_unusable_gate_file_reads_as_no_gate(tmp_path: Path, raw: str) -> None:
    """
    A truncated or malformed record fails to the server, never open.

    A hook subprocess can read this file while it is being replaced, and a
    crash can leave a partial one. Fails if a bad file raises (the hook
    would fail closed and block a legitimate tool) or is read as permission
    to skip.
    """
    (tmp_path / GATE_FILE).write_text(raw, encoding="utf-8")
    assert read_gate(tmp_path) is None
    assert may_skip_policy_call(tmp_path) is False


def test_recording_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    """
    The record is swapped in atomically, with nothing left over.

    Hook subprocesses read this file concurrently, so a half-written record
    must never be observable. Fails if the write is in place, or if temp
    files accumulate in the bridge directory.
    """
    for _ in range(3):
        record_gate(tmp_path, _AFFIRMATIVE)
    assert [p.name for p in tmp_path.iterdir()] == [GATE_FILE]
    assert json.loads((tmp_path / GATE_FILE).read_text(encoding="utf-8"))["no_policies"] is True


def test_clearing_by_session_id_finds_the_registered_bridge_dir(tmp_path: Path) -> None:
    """
    The runner can revoke a gate knowing only the session id.

    That is the whole invalidation path: the server reports a policy
    change against a session, and the runner must clear the gate the relay
    wrote. Fails if the mapping isn't recorded on write, in which case a
    newly added policy wouldn't be enforced until the gate expired.
    """
    record_gate(tmp_path, _AFFIRMATIVE, session_id="conv_abc")
    assert may_skip_policy_call(tmp_path) is True
    try:
        assert clear_gate_for_session("conv_abc") is True
        assert may_skip_policy_call(tmp_path) is False
        # A session nobody registered reports the miss, so the caller knows
        # the gate will lapse on expiry rather than now.
        assert clear_gate_for_session("conv_unknown") is False
    finally:
        forget_session("conv_abc")


def test_clearing_and_reading_a_missing_gate_never_raise(tmp_path: Path) -> None:
    """
    The no-gate path is quiet: no file, no error, no skip.

    Every session starts here, and this runs on the harness's blocking
    path. Fails if a missing file raises into a hook.
    """
    clear_gate(tmp_path)
    assert read_gate(tmp_path) is None
    assert may_skip_policy_call(tmp_path) is False
    note_session_bridge_dir("conv_gone", tmp_path / "nope")
    try:
        assert clear_gate_for_session("conv_gone") is True
    finally:
        forget_session("conv_gone")


def test_default_ttl_stays_under_the_servers_default_policy_staleness() -> None:
    """
    Expiry is tighter than the staleness the evaluate path already carries.

    Expiry is the only bound that always holds — the push that clears a gate
    rides the session's runner tunnel, so a policy mutation landing on
    another replica never reaches it. Keeping the window well under the
    server's own 30s default-policy cache is what makes that fallback
    acceptable. Fails if the gate's window creeps up to or past the number
    the module docstring measures it against.
    """
    from omnigent.runtime.policies.builder import _DEFAULT_POLICY_SPECS_CACHE

    assert 0 < GATE_TTL_S < _DEFAULT_POLICY_SPECS_CACHE.ttl


def test_gate_live_requires_both_affirmative_and_unexpired() -> None:
    """
    ``PolicyGate.live`` is the single place the two conditions meet.

    Fails if either half is dropped, which is how a gate would outlive its
    expiry or a negative record would start licensing skips.
    """
    assert PolicyGate(no_policies=True, expires_at=100.0).live(now=99.0) is True
    assert PolicyGate(no_policies=True, expires_at=100.0).live(now=101.0) is False
    assert PolicyGate(no_policies=False, expires_at=100.0).live(now=99.0) is False
