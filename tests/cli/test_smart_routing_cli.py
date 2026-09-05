"""Tests for CLI-side Smart Routing: preflight, the armed create, and launch."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from click import ClickException, UsageError
from click.testing import CliRunner

from omnigent.cli import _reject_smart_routing_prompt, _smart_routing_decision, cli
from omnigent.smart_routing_cli import (
    ROUTING_SESSION_LABELS,
    arm_smart_routing_session,
    check_smart_routing_available,
    known_host_id,
)

_BASE = "http://localhost:6767"
_HOST_ID = "host_abc123"
_SESSION_ID = "conv_routed"


@pytest.fixture(autouse=True)
def _no_local_gateway_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the machine-local gateway check to "unknown" for every test.

    The preflight resolves this machine's own harness configs, so leaving it
    live would make every case depend on the developer's ``~/.omnigent``. Cases
    about the local gate override it explicitly.
    """
    monkeypatch.setattr("omnigent.smart_routing_cli.local_gateway_inference", dict)


def _mock_local_gateway(monkeypatch: pytest.MonkeyPatch, gateway: dict[str, bool]) -> None:
    monkeypatch.setattr(
        "omnigent.smart_routing_cli.local_gateway_inference", lambda: dict(gateway)
    )


def _mock_info(
    *,
    enabled: bool = True,
    external: bool | None = None,
    oss: bool = False,
    omit_sources: bool = False,
) -> None:
    """Mock ``GET /v1/info``: whether routing is on, and which sources serve it.

    The default is the shape a gateway workspace reports — the external
    AI-Gateway router and no built-in judge — so a family off the gateway is a
    hard error. ``omit_sources`` reproduces a server from before the field
    existed.
    """
    payload: dict[str, Any] = {"smart_routing_enabled": enabled}
    if not omit_sources:
        payload["smart_routing_sources"] = {
            "external": enabled if external is None else external,
            "oss": oss,
        }
    respx.get(f"{_BASE}/v1/info").mock(return_value=httpx.Response(200, json=payload))


def _mock_hosts(gateway: dict[str, Any] | None) -> None:
    host: dict[str, Any] = {"host_id": _HOST_ID, "status": "online"}
    if gateway is not None:
        host["gateway_inference"] = gateway
    respx.get(f"{_BASE}/v1/hosts").mock(return_value=httpx.Response(200, json={"hosts": [host]}))


def _mock_create(**session: Any) -> respx.Route:
    """Mock the armed create with one session body."""
    body = {"id": _SESSION_ID, **session}
    return respx.post(f"{_BASE}/v1/sessions").mock(return_value=httpx.Response(201, json=body))


# ── preflight ────────────────────────────────────────────────────────────


@respx.mock
def test_preflight_passes_when_routing_enabled_and_gateway_absent() -> None:
    """An absent ``gateway_inference`` map is unknown, and unknown does not gate."""
    _mock_info()
    _mock_hosts(None)

    check_smart_routing_available(base_url=_BASE, harnesses=("claude-native",), host_id=_HOST_ID)


@respx.mock
def test_preflight_rejects_when_server_cannot_route() -> None:
    """No routing client on the server is a hard error naming the reason."""
    _mock_info(enabled=False)

    with pytest.raises(ClickException, match="no routing model configured"):
        check_smart_routing_available(
            base_url=_BASE, harnesses=("claude-native",), host_id=_HOST_ID
        )


@respx.mock
def test_preflight_rejects_when_host_inference_is_not_gateway_backed() -> None:
    """A routed model the pane cannot reach is worse than no pick — fail loud."""
    _mock_info()
    _mock_hosts({"claude-native": False, "codex-native": True})

    with pytest.raises(ClickException, match=r"claude-native.*not AI-Gateway-backed"):
        check_smart_routing_available(
            base_url=_BASE, harnesses=("claude-native",), host_id=_HOST_ID
        )


@respx.mock
def test_preflight_reports_the_hosts_own_reason_string() -> None:
    """A string entry carries the host's reason into the error text."""
    _mock_info()
    _mock_hosts({"codex-native": "provider-not-gateway"})

    with pytest.raises(ClickException, match="provider-not-gateway"):
        check_smart_routing_available(
            base_url=_BASE, harnesses=("codex-native",), host_id=_HOST_ID
        )


@respx.mock
def test_preflight_ignores_other_families_than_the_requested_one() -> None:
    """A per-harness route only needs its own family to be gateway-backed."""
    _mock_info()
    _mock_hosts({"claude-native": True, "codex-native": False})

    check_smart_routing_available(base_url=_BASE, harnesses=("claude-native",), host_id=_HOST_ID)


@respx.mock
def test_preflight_keys_off_harness_spellings_not_bare_families() -> None:
    """``gateway_inference`` is keyed per harness spelling; ``claude`` is not one."""
    _mock_info()
    _mock_hosts({"claude": False})

    # A bare-family key is not in the map's vocabulary, so it must read as "no
    # entry" (unknown) rather than gate the launch.
    check_smart_routing_available(base_url=_BASE, harnesses=("claude-native",), host_id=_HOST_ID)


@respx.mock
def test_preflight_reads_the_spelling_it_was_asked_about() -> None:
    """The map carries every spelling, so the caller's own is a valid key."""
    _mock_info()
    _mock_hosts({"native-claude": False})

    with pytest.raises(ClickException, match="not AI-Gateway-backed"):
        check_smart_routing_available(
            base_url=_BASE, harnesses=("native-claude",), host_id=_HOST_ID
        )


# ── preflight: the machine-local gate ────────────────────────────────────
#
# A ``--smart-routing`` launch runs its TUI on THIS machine, so the local config
# resolution is the authoritative answer about whether a routed model would be
# reachable — and unlike the host row it is available before the daemon has
# registered, which is exactly when the server-side gate reads "unknown".

#: The four credential states the gating table pins, as this machine reports
#: them: (state, local map, the harness that must still route).
_LOCAL_GATEWAY_STATES: list[tuple[str, dict[str, bool], str | None]] = [
    ("A: neither harness on the gateway", {"claude-native": False, "codex-native": False}, None),
    (
        "B: claude on the gateway, codex on a ChatGPT subscription",
        {"claude-native": True, "codex-native": False},
        "claude-native",
    ),
    (
        "C: codex on the gateway, claude on a subscription",
        {"claude-native": False, "codex-native": True},
        "codex-native",
    ),
    ("D: both on the gateway", {"claude-native": True, "codex-native": True}, "both"),
]


def _assert_downgrade_notice(
    capsys: pytest.CaptureFixture[str], *, expected: tuple[str, ...]
) -> None:
    """Assert the built-in-router line was printed for exactly *expected*."""
    err = capsys.readouterr().err
    named = tuple(h for h in ("claude-native", "codex-native") if h in err)
    assert named == expected, err
    assert err.count("routing with the built-in router instead") == len(expected), err


@respx.mock
@pytest.mark.parametrize("oss", [False, True], ids=["no-built-in-judge", "built-in-judge"])
@pytest.mark.parametrize(("state", "local", "routable"), _LOCAL_GATEWAY_STATES)
def test_preflight_gates_each_harness_on_the_local_gateway_answer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: str,
    local: dict[str, bool],
    routable: str | None,
    oss: bool,
) -> None:
    """Off-gateway selects the source: the built-in judge serves it, or it fails."""
    _mock_info(oss=oss)
    # No host row at all — the local answer must gate on its own.
    _mock_hosts(None)
    _mock_local_gateway(monkeypatch, local)

    for harness in ("claude-native", "codex-native"):
        gatewayed = routable in (harness, "both")
        if not gatewayed and not oss:
            with pytest.raises(ClickException, match=f"unavailable for {harness}"):
                check_smart_routing_available(base_url=_BASE, harnesses=(harness,), host_id=None)
            continue
        check_smart_routing_available(base_url=_BASE, harnesses=(harness,), host_id=None)
        _assert_downgrade_notice(capsys, expected=() if gatewayed else (harness,))


@respx.mock
def test_preflight_degrades_an_older_servers_missing_sources_field_to_both(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A server reporting no sources at all, but able to route, is assumed able to
    # serve either one — so an off-gateway family downgrades instead of failing.
    _mock_info(omit_sources=True)
    _mock_local_gateway(monkeypatch, {"codex-native": False})

    check_smart_routing_available(base_url=_BASE, harnesses=("codex-native",), host_id=None)

    _assert_downgrade_notice(capsys, expected=("codex-native",))


@respx.mock
def test_preflight_rejects_when_neither_source_can_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Availability is the sources, not the legacy flag: with both false there is
    # nothing to route with, whatever smart_routing_enabled claims.
    _mock_info(enabled=True, external=False, oss=False)
    _mock_local_gateway(monkeypatch, {"claude-native": True})

    with pytest.raises(ClickException, match="no routing model configured"):
        check_smart_routing_available(base_url=_BASE, harnesses=("claude-native",), host_id=None)


@respx.mock
def test_preflight_gates_without_a_registered_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local answer gates even when the server has never seen this host.

    Before the daemon registers there is no ``gateway_inference`` row to read,
    and the old server-only gate let a routed launch through on a pane that
    could not reach the pick.
    """
    _mock_info()
    _mock_local_gateway(monkeypatch, {"codex-native": False})

    with pytest.raises(ClickException, match=r"codex-native.*not AI-Gateway-backed"):
        check_smart_routing_available(base_url=_BASE, harnesses=("codex-native",), host_id=None)


@respx.mock
def test_preflight_local_answer_wins_over_a_stale_host_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TUI launches here, so this machine's own config decides."""
    _mock_info()
    _mock_hosts({"codex-native": True})
    _mock_local_gateway(monkeypatch, {"codex-native": False})

    with pytest.raises(ClickException, match="not AI-Gateway-backed"):
        check_smart_routing_available(
            base_url=_BASE, harnesses=("codex-native",), host_id=_HOST_ID
        )


@respx.mock
def test_preflight_falls_back_to_the_host_row_for_an_unanswered_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A family the local check could not evaluate is omitted, not False."""
    _mock_info()
    _mock_hosts({"codex-native": False})
    # gateway_inference_map omits an unevaluable family, so codex is absent here.
    _mock_local_gateway(monkeypatch, {"claude-native": True})

    with pytest.raises(ClickException, match="not AI-Gateway-backed"):
        check_smart_routing_available(
            base_url=_BASE, harnesses=("codex-native",), host_id=_HOST_ID
        )


@respx.mock
def test_preflight_routing_disabled_is_reported_before_the_gateway_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State A's two causes read differently: no router beats no gateway."""
    _mock_info(enabled=False)
    _mock_local_gateway(monkeypatch, {"claude-native": False, "codex-native": False})

    with pytest.raises(ClickException, match="no routing model configured"):
        check_smart_routing_available(base_url=_BASE, harnesses=("claude-native",), host_id=None)


def test_local_gateway_inference_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unevaluable local map is unknown, and unknown does not gate."""
    from omnigent import smart_routing_cli

    def _boom() -> dict[str, bool]:
        raise RuntimeError("no config")

    monkeypatch.setattr("omnigent.gateway_inference.gateway_inference_map", _boom)

    assert smart_routing_cli.local_gateway_inference() == {}


# ── the armed create ─────────────────────────────────────────────────────


@respx.mock
def test_arming_sends_the_mode_but_never_a_message() -> None:
    """The CLI arms the session; the harness routes the first typed message."""
    route = _mock_create(harness="claude-native")

    armed = arm_smart_routing_session(
        base_url=_BASE,
        harness="claude-native",
        host_id=_HOST_ID,
        workspace="/repo",
    )

    payload = json.loads(route.calls.last.request.content)
    # The mode override is what arms Smart Routing (and what the hook's
    # server-side gate reads); a message would route at create time, which is
    # the web UI's job, not the CLI's.
    assert payload["cost_control_mode_override"] == "on"
    assert "smart_routing_message" not in payload
    assert "harness_override" not in payload
    assert payload["host_type"] == "external"
    # Bound to the launch host + its workspace: the pane's model catalog comes
    # from that host, and the server 400s a host_id with no workspace.
    assert payload["host_id"] == _HOST_ID
    assert payload["workspace"] == "/repo"
    assert payload["labels"] == ROUTING_SESSION_LABELS
    assert armed == type(armed)(session_id=_SESSION_ID, notice=None)


@respx.mock
def test_arming_omits_the_workspace_when_there_is_no_host() -> None:
    """No host means no workspace either — the server validates them together."""
    route = _mock_create(harness="claude-native")

    arm_smart_routing_session(base_url=_BASE, harness="claude-native")

    payload = json.loads(route.calls.last.request.content)
    assert "host_id" not in payload
    assert "workspace" not in payload


@respx.mock
def test_arming_binds_the_requested_harnesss_own_wrapper() -> None:
    """Each harness arms its own built-in wrapper agent, never a placeholder."""
    from omnigent.db.utils import builtin_agent_id

    route = _mock_create(harness="codex-native")

    arm_smart_routing_session(base_url=_BASE, harness="codex-native")

    payload = json.loads(route.calls.last.request.content)
    assert payload["agent_id"] == builtin_agent_id("codex-native-ui")


@respx.mock
def test_arming_never_deletes_the_session_it_created() -> None:
    """The armed session IS the session — the wrapper attaches to it."""
    deleted = respx.delete(f"{_BASE}/v1/sessions/{_SESSION_ID}").mock(
        return_value=httpx.Response(204)
    )
    _mock_create(harness="claude-native")

    arm_smart_routing_session(base_url=_BASE, harness="claude-native")

    assert not deleted.called


# A create that is rejected, unreachable, or missing a session id never blocks
# the launch: it returns no session and a notice.
@respx.mock
@pytest.mark.parametrize(
    ("mock_kwargs", "notice_contains"),
    [
        ({"return_value": httpx.Response(400, text="runner is offline")}, "400"),
        ({"side_effect": httpx.ConnectError("refused")}, "could not reach"),
        ({"return_value": httpx.Response(201, json={})}, "no session id"),
    ],
)
def test_arming_fails_open(mock_kwargs: dict[str, Any], notice_contains: str) -> None:
    respx.post(f"{_BASE}/v1/sessions").mock(**mock_kwargs)

    armed = arm_smart_routing_session(base_url=_BASE, harness="claude-native")

    assert armed.session_id is None
    assert armed.notice is not None
    assert notice_contains in armed.notice


# ── preflight + arm, as the subcommands call it ──────────────────────────


@pytest.fixture
def _routing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the launch helpers at a fake backend, daemon, and host identity."""
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda _s: _BASE)
    monkeypatch.setattr("omnigent.cli._ensure_host_daemon", lambda _s: False)
    monkeypatch.setattr(
        "omnigent.host.identity.load_or_create_host_identity",
        lambda: type("_Id", (), {"host_id": _HOST_ID})(),
    )


@respx.mock
def test_the_decision_binds_the_launch_host_and_cwd(_routing_env: None) -> None:
    """The armed session runs here, so it carries this host and this workspace."""
    _mock_info()
    _mock_hosts({"claude-native": True})
    route = _mock_create(harness="claude-native")

    armed = _smart_routing_decision(server=_BASE, harness="claude-native")

    assert armed.session_id == _SESSION_ID
    payload = json.loads(route.calls.last.request.content)
    assert payload["host_id"] == _HOST_ID
    assert payload["workspace"] == str(Path.cwd().resolve())


@respx.mock
def test_the_decision_routes_hostlessly_when_the_server_does_not_know_the_host(
    _routing_env: None,
) -> None:
    """An unregistered host degrades to a hostless create, not a failed one."""
    _mock_info()
    respx.get(f"{_BASE}/v1/hosts").mock(return_value=httpx.Response(200, json={"hosts": []}))
    route = _mock_create(harness="claude-native")

    _smart_routing_decision(server=_BASE, harness="claude-native")

    payload = json.loads(route.calls.last.request.content)
    assert "host_id" not in payload
    assert "workspace" not in payload


def test_known_host_id_requires_the_server_to_have_seen_the_host() -> None:
    with respx.mock:
        _mock_hosts(None)
        assert known_host_id(base_url=_BASE, host_id=_HOST_ID) == _HOST_ID
        assert known_host_id(base_url=_BASE, host_id="host_other") is None
        assert known_host_id(base_url=_BASE, host_id=None) is None


@respx.mock
def test_the_routing_preflight_reads_are_not_on_the_creates_long_budget() -> None:
    """
    A wedged server cannot stall the launch before the session exists.

    The preflight GETs already degrade to "unknown", which does not gate, so a
    long wait buys nothing; the create's own 60s read budget is for the create,
    which does real work. Asserted against the constants so the two stay
    distinguishable.
    """
    from omnigent.smart_routing_cli import _PREFLIGHT_TIMEOUT, _TIMEOUT

    assert _PREFLIGHT_TIMEOUT.read is not None
    assert _PREFLIGHT_TIMEOUT.connect is not None
    assert _PREFLIGHT_TIMEOUT.read <= 8.0
    assert _TIMEOUT.read is not None
    assert _PREFLIGHT_TIMEOUT.read < _TIMEOUT.read


# ── rejected combinations ────────────────────────────────────────────────


def test_reject_prompt_only_fires_on_real_text() -> None:
    # Whitespace is not a prompt; a bare launch must stay servable.
    assert _reject_smart_routing_prompt(None) is None
    assert _reject_smart_routing_prompt("   ") is None
    with pytest.raises(UsageError, match="cannot be combined with -p"):
        _reject_smart_routing_prompt("fix the flaky test")


@pytest.mark.parametrize(
    "args",
    [
        ["claude", "--smart-routing", "-p", "fix the flaky test"],
        ["claude", "--smart-routing", "--prompt", "fix the flaky test"],
        ["codex", "--smart-routing", "-p", "port the parser"],
    ],
)
def test_a_prompt_with_smart_routing_is_rejected(args: list[str]) -> None:
    # The CLI routes what you type in the TUI, so a create-time prompt has no
    # path — say where it does have one instead of launching unrouted.
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 2, result.output
    assert "cannot be combined with -p/--prompt" in result.output
    assert "web UI" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["claude", "--smart-routing", "--resume", "conv_old"],
        ["codex", "--smart-routing", "--resume", "conv_old"],
        ["claude", "--smart-routing", "--session", "conv_old"],
    ],
)
def test_smart_routing_with_a_resume_is_rejected(args: list[str]) -> None:
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 1, result.output
    assert "routes a new session" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["run", "--smart-routing"],
        ["run", "--smart-routing", "-p", "review the last commit"],
        ["run", "--harness", "claude-native", "--smart-routing", "-p", "hi"],
    ],
)
def test_run_smart_routing_is_removed(args: list[str]) -> None:
    # `run` never routed from inside a harness, and its create-time route is
    # gone — the rejection has to name both surfaces that still route.
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 1, result.output
    assert "per-harness first-message only" in result.output
    assert "omnigent claude --smart-routing" in result.output
    assert "web UI" in result.output


def test_run_no_longer_advertises_smart_routing() -> None:
    result = CliRunner().invoke(cli, ["run", "--help"])

    assert result.exit_code == 0, result.output
    assert "--smart-routing" not in result.output


# ── the dedicated subcommands ────────────────────────────────────────────


@respx.mock
@pytest.mark.parametrize(
    ("command", "launcher"),
    [
        ("claude", "omnigent.claude_native.run_claude_native"),
        ("codex", "omnigent.codex_native.run_codex_native"),
    ],
)
def test_a_subcommand_arms_the_session_and_launches_bare(
    command: str, launcher: str, monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    # `omnigent claude|codex --smart-routing` takes no -p: the hook routes.
    _mock_info()
    _mock_hosts({f"{command}-native": True})
    route = _mock_create(harness=f"{command}-native")
    captured: dict[str, Any] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr(launcher, lambda **kw: captured.update(kw))

    result = CliRunner().invoke(cli, [command, "--smart-routing"])

    assert result.exit_code == 0, result.output
    payload = json.loads(route.calls.last.request.content)
    assert payload["cost_control_mode_override"] == "on"
    assert "smart_routing_message" not in payload
    # Attach, not bundle: the armed session carries the mode, the wrapper labels
    # and the decision card the server wrote at create.
    assert captured["session_id"] == _SESSION_ID
    assert captured["prompt"] is None
    # Nothing is picked at create, so no routed model reaches the wrapper.
    assert captured["extra_args"] == ()
    assert captured.get("model") is None
    assert "Smart Routing is on for this session" in result.output


@respx.mock
def test_an_explicit_model_survives_arming(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    """Arming picks nothing, so a model the user typed reaches the launch."""
    _mock_info()
    _mock_hosts(None)
    _mock_create(harness="codex-native")
    captured: dict[str, Any] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.codex_native.run_codex_native", lambda **kw: captured.update(kw))

    result = CliRunner().invoke(cli, ["codex", "--smart-routing", "--model", "gpt-5.4"])

    assert result.exit_code == 0, result.output
    assert captured["model"] == "gpt-5.4"


@respx.mock
def test_a_subcommand_falls_back_to_a_fresh_session(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    """A rejected create leaves the wrapper to bundle its own session."""
    _mock_info()
    _mock_hosts(None)
    respx.post(f"{_BASE}/v1/sessions").mock(return_value=httpx.Response(500, text="boom"))
    captured: dict[str, Any] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native", lambda **kw: captured.update(kw)
    )

    result = CliRunner().invoke(cli, ["claude", "--smart-routing"])

    assert result.exit_code == 0, result.output
    assert captured["session_id"] is None
    assert "Smart Routing was unavailable" in result.output


@respx.mock
def test_a_create_rejected_for_a_non_routing_reason_says_so_and_still_launches(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    """
    A 400 that has nothing to do with routing is announced, not swallowed.

    Live evidence: a routed create rejected with "runner is offline" dropped
    the CLI into a plain wrapper session with no notice at all, so the user
    typed at a session that was quietly ignoring the ``--smart-routing`` they
    had asked for. The fallback is right — routing must never be fatal — but it
    has to be visible, and the reason has to reach stderr.
    """
    _mock_info()
    _mock_hosts(None)
    respx.post(f"{_BASE}/v1/sessions").mock(
        return_value=httpx.Response(400, text="runner is offline")
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native", lambda **kw: captured.update(kw)
    )

    result = CliRunner().invoke(cli, ["claude", "--smart-routing"])

    # Not fatal.
    assert result.exit_code == 0, result.output
    # One explicit line, naming the status the server answered with.
    assert "Smart Routing was unavailable" in result.output
    assert "400" in result.output
    # And the session still launches, unrouted.
    assert captured["session_id"] is None
    assert captured["extra_args"] == ()


@respx.mock
def test_an_unavailable_preflight_blocks_the_launch(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    """An unavailable-routing error must stop before any wrapper runs."""
    _mock_info(enabled=False)
    _mock_hosts(None)

    def _must_not_launch(**_kwargs: Any) -> None:
        raise AssertionError("wrapper launched despite unavailable routing")

    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.claude_native.run_claude_native", _must_not_launch)

    result = CliRunner().invoke(cli, ["claude", "--smart-routing"])

    assert result.exit_code == 1, result.output
    assert "Smart Routing is not enabled" in result.output


# ── credential-provider gating, end to end ───────────────────────────────
#
# The shape a Databricks user actually has: Claude Code pointed at the workspace
# AI Gateway, Codex signed in to a personal ChatGPT subscription. Routing is
# applied by rewriting the launch model to a gateway catalog id, so only the
# Claude pane can honour it — a routed launch into Codex must fail fast.

_CODEX_ON_SUBSCRIPTION = {"claude-native": True, "codex-native": False}
_NO_GATEWAY_AT_ALL = {"claude-native": False, "codex-native": False}


@respx.mock
def test_claude_route_survives_codex_being_on_a_subscription() -> None:
    """Per-family gating: the arm that IS on the gateway keeps its routing."""
    _mock_info()
    _mock_hosts(_CODEX_ON_SUBSCRIPTION)

    check_smart_routing_available(
        base_url=_BASE,
        harnesses=("claude-native",),
        host_id=_HOST_ID,
    )


@respx.mock
@pytest.mark.parametrize(
    ("args", "gateway"),
    [
        (["codex", "--smart-routing"], _CODEX_ON_SUBSCRIPTION),
        # No AI Gateway anywhere: every entry point errors, Claude included.
        (["claude", "--smart-routing"], _NO_GATEWAY_AT_ALL),
        (["codex", "--smart-routing"], _NO_GATEWAY_AT_ALL),
    ],
)
def test_smart_routing_entry_points_error_on_an_ungatewayed_harness(
    monkeypatch: pytest.MonkeyPatch,
    _routing_env: None,
    args: list[str],
    gateway: dict[str, Any],
) -> None:
    _mock_info()
    _mock_hosts(gateway)

    def _must_not_launch(**_kwargs: Any) -> None:
        raise AssertionError("wrapper launched despite ungatewayed inference")

    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.claude_native.run_claude_native", _must_not_launch)
    monkeypatch.setattr("omnigent.codex_native.run_codex_native", _must_not_launch)

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 1, result.output
    assert "not AI-Gateway-backed" in result.output
    # The error names the harness and the way out, not just "unavailable".
    assert "omnigent configure harnesses" in result.output
    # Nothing was created: a pick that cannot be applied is not attempted, so
    # preflight's reads are the only traffic.
    assert all(call.request.method == "GET" for call in respx.calls)
