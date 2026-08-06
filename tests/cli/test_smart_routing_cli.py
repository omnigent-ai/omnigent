"""Tests for CLI-side Smart Routing: preflight, the routed create, and launch."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from click import ClickException, UsageError
from click.testing import CliRunner

from omnigent.cli import (
    _dispatch_smart_routing,
    _require_smart_routing_prompt,
    _run_smart_routing,
    _smart_routing_capable_harness,
    _with_routed_model_arg,
    cli,
)
from omnigent.smart_routing_cli import (
    ROUTING_SESSION_LABELS,
    check_smart_routing_available,
    create_smart_routing_session,
    known_host_id,
    smart_routing_families,
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
    """Mock the routed create (and its snapshot re-read) with one session body."""
    body = {"id": _SESSION_ID, **session}
    respx.get(f"{_BASE}/v1/sessions/{_SESSION_ID}").mock(
        return_value=httpx.Response(200, json=body)
    )
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
    """A fixed-harness route only needs its own family to be gateway-backed."""
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


def test_auto_route_requires_both_native_families() -> None:
    assert smart_routing_families(None) == ("claude-native", "codex-native")
    assert smart_routing_families("auto") == ("claude-native", "codex-native")
    assert smart_routing_families("codex-native") == ("codex-native",)


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
    # The auto route picks across both arms, so one arm off the gateway takes
    # it — unless the built-in judge can answer for that arm instead.
    auto = smart_routing_families(None)
    off_gateway = tuple(harness for harness in auto if local[harness] is False)
    if off_gateway and not oss:
        with pytest.raises(ClickException, match="not AI-Gateway-backed"):
            check_smart_routing_available(base_url=_BASE, harnesses=auto, host_id=None)
    else:
        check_smart_routing_available(base_url=_BASE, harnesses=auto, host_id=None)
        _assert_downgrade_notice(capsys, expected=off_gateway)


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


# ── the routed create ────────────────────────────────────────────────────


@respx.mock
def test_create_sends_the_routing_contract_for_a_fixed_harness() -> None:
    """Tier 2 binds the wrapper agent; the harness needs no override."""
    route = _mock_create(harness="claude-native", model_override="claude-sonnet-5")

    decision = create_smart_routing_session(
        base_url=_BASE,
        prompt="fix the flaky test",
        harness="claude-native",
        host_id=_HOST_ID,
        workspace="/repo",
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload["cost_control_mode_override"] == "on"
    assert payload["smart_routing_message"] == "fix the flaky test"
    assert payload["host_type"] == "external"
    # Bound to the launch host + its workspace: the router's candidate catalog
    # comes from that host's model options, and the server 400s a host_id with
    # no workspace.
    assert payload["host_id"] == _HOST_ID
    assert payload["workspace"] == "/repo"
    assert payload["labels"] == ROUTING_SESSION_LABELS
    # A fixed harness comes from the bound wrapper agent, so no override is
    # sent — only the auto route uses the sentinel.
    assert "harness_override" not in payload
    assert (decision.session_id, decision.model) == (_SESSION_ID, "claude-sonnet-5")
    assert decision.notice is None


@respx.mock
def test_create_asks_for_a_harness_on_the_auto_route() -> None:
    """Tier 3 sends the ``auto`` sentinel and reads the bound harness back."""
    route = _mock_create(harness="codex-native", model_override="gpt-5.4")

    decision = create_smart_routing_session(
        base_url=_BASE,
        prompt="port the parser",
        harness=None,
        host_id=_HOST_ID,
        workspace="/repo",
    )

    assert json.loads(route.calls.last.request.content)["harness_override"] == "auto"
    assert (decision.harness, decision.model) == ("codex-native", "gpt-5.4")


@respx.mock
def test_create_omits_the_workspace_when_there_is_no_host() -> None:
    """No host means no workspace either — the server validates them together."""
    route = _mock_create(harness="claude-native", model_override="claude-sonnet-5")

    create_smart_routing_session(base_url=_BASE, prompt="hello", harness="claude-native")

    payload = json.loads(route.calls.last.request.content)
    assert "host_id" not in payload
    assert "workspace" not in payload


@respx.mock
def test_create_never_deletes_the_routed_session() -> None:
    """The routed session IS the session — the wrapper attaches to it."""
    deleted = respx.delete(f"{_BASE}/v1/sessions/{_SESSION_ID}").mock(
        return_value=httpx.Response(204)
    )
    _mock_create(harness="claude-native", model_override="claude-sonnet-5")

    create_smart_routing_session(base_url=_BASE, prompt="hello", harness="claude-native")

    assert not deleted.called


@respx.mock
def test_create_falls_back_to_the_session_snapshot() -> None:
    """A create response without the verdict is re-read from the snapshot."""
    respx.post(f"{_BASE}/v1/sessions").mock(
        return_value=httpx.Response(201, json={"id": _SESSION_ID})
    )
    respx.get(f"{_BASE}/v1/sessions/{_SESSION_ID}").mock(
        return_value=httpx.Response(
            200, json={"harness": "codex-native", "model_override": "gpt-5.4-mini"}
        )
    )

    decision = create_smart_routing_session(base_url=_BASE, prompt="hello", harness=None)

    assert (decision.harness, decision.model) == ("codex-native", "gpt-5.4-mini")


@respx.mock
def test_create_without_a_prompt_only_turns_smart_routing_on() -> None:
    # A bare create arms the session; the in-harness hook picks the model.
    route = _mock_create(harness="claude-native")

    decision = create_smart_routing_session(
        base_url=_BASE, prompt=None, harness="claude-native", host_id=_HOST_ID, workspace="/repo"
    )

    payload = json.loads(route.calls.last.request.content)
    # The mode override is what arms Smart Routing (and what the hook's
    # server-side gate reads); the message is what would route at create time.
    assert payload["cost_control_mode_override"] == "on"
    assert "smart_routing_message" not in payload
    assert payload["host_id"] == _HOST_ID
    assert payload["workspace"] == "/repo"
    assert payload["labels"] == ROUTING_SESSION_LABELS
    assert decision.session_id == _SESSION_ID
    # Nothing is routed yet, so an empty pick is the expected answer — not a
    # "did not pick a model" notice.
    assert decision.model is None
    assert decision.notice is None


@respx.mock
def test_create_keeps_the_session_when_no_model_was_picked() -> None:
    """Fail-open: the session still launches, on the harness default model."""
    _mock_create(harness="claude-native", model_override=None)

    decision = create_smart_routing_session(
        base_url=_BASE, prompt="hello", harness="claude-native"
    )

    assert decision.session_id == _SESSION_ID
    assert decision.model is None
    assert decision.notice is not None
    assert "did not pick a model" in decision.notice


# A create that is rejected, unreachable, or missing a session id never blocks
# the launch: it returns no session and a notice.
@respx.mock
@pytest.mark.parametrize(
    ("mock_kwargs", "harness", "notice_contains"),
    [
        ({"return_value": httpx.Response(400, text="no native CLI")}, None, "400"),
        ({"side_effect": httpx.ConnectError("refused")}, "claude-native", "could not reach"),
        ({"return_value": httpx.Response(201, json={})}, "claude-native", None),
    ],
)
def test_create_fails_open(
    mock_kwargs: dict[str, Any], harness: str | None, notice_contains: str | None
) -> None:
    respx.post(f"{_BASE}/v1/sessions").mock(**mock_kwargs)

    decision = create_smart_routing_session(base_url=_BASE, prompt="hello", harness=harness)

    assert (decision.session_id, decision.harness, decision.model) == (None, None, None)
    assert decision.notice is not None
    if notice_contains is not None:
        assert notice_contains in decision.notice


# ── dispatch ─────────────────────────────────────────────────────────────


@pytest.fixture
def _routing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the dispatch helpers at a fake backend, daemon, and host identity."""
    monkeypatch.setattr("omnigent.cli._ensure_backend", lambda _s: _BASE)
    monkeypatch.setattr("omnigent.cli._ensure_host_daemon", lambda _s: False)
    monkeypatch.setattr(
        "omnigent.host.identity.load_or_create_host_identity",
        lambda: type("_Id", (), {"host_id": _HOST_ID})(),
    )


@respx.mock
def test_dispatch_tier2_attaches_the_wrapper_to_the_routed_session(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    """A fixed harness keeps its wrapper, gains ``--model``, and attaches."""
    _mock_info()
    _mock_hosts({"claude-native": True})
    route = _mock_create(harness="claude-native", model_override="claude-opus-4-7")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native", lambda **kw: captured.update(kw)
    )

    _dispatch_smart_routing(
        harness="claude-native",
        server=None,
        prompt="fix the flaky test",
        model=None,
        auto_open_conversation=False,
    )

    # Attach, not bundle: the routed session already carries the model, the
    # decision card, and the wrapper labels the server wrote at create.
    assert captured["session_id"] == _SESSION_ID
    assert captured["extra_args"] == ("--model", "claude-opus-4-7")
    assert captured["prompt"] == "fix the flaky test"
    payload = json.loads(route.calls.last.request.content)
    assert payload["host_id"] == _HOST_ID
    assert payload["workspace"] == str(Path.cwd().resolve())


@respx.mock
def test_dispatch_tier3_launches_the_harness_the_server_bound(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    """The auto route picks the harness; the CLI execs that wrapper on it."""
    _mock_info()
    _mock_hosts(None)
    _mock_create(harness="codex-native", model_override="gpt-5.4")
    captured: dict[str, Any] = {}
    monkeypatch.setattr("omnigent.codex_native.run_codex_native", lambda **kw: captured.update(kw))

    _dispatch_smart_routing(
        harness=None,
        server=None,
        prompt="port the parser",
        model=None,
        auto_open_conversation=False,
    )

    assert captured["session_id"] == _SESSION_ID
    assert captured["model"] == "gpt-5.4"
    assert captured["prompt"] == "port the parser"


@respx.mock
def test_dispatch_launches_bare_when_the_harness_routes_its_own_first_message(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # No -p on a hooked harness: arm the session, then let the TUI route.
    _mock_info()
    _mock_hosts({"claude-native": True})
    route = _mock_create(harness="claude-native")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native", lambda **kw: captured.update(kw)
    )

    _dispatch_smart_routing(
        harness="claude-native",
        server=None,
        prompt=None,
        model=None,
        auto_open_conversation=False,
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload["cost_control_mode_override"] == "on"
    assert "smart_routing_message" not in payload
    assert captured["session_id"] == _SESSION_ID
    # Nothing was picked at create, so the launch carries no routed --model and
    # no initial prompt — the hook applies the model on the first typed message.
    assert captured["extra_args"] == ()
    assert captured["prompt"] is None
    assert "Smart Routing is on for this session" in capsys.readouterr().err


@respx.mock
def test_dispatch_bare_preflights_the_pinned_harness(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    # A bare launch that cannot be routed fails loud, not silently unrouted.
    _mock_info()
    _mock_hosts({"claude-native": False})

    def _must_not_launch(**_kwargs: Any) -> None:
        raise AssertionError("wrapper launched despite unavailable routing")

    monkeypatch.setattr("omnigent.claude_native.run_claude_native", _must_not_launch)

    with pytest.raises(ClickException, match="not AI-Gateway-backed"):
        _dispatch_smart_routing(
            harness="claude-native",
            server=None,
            prompt=None,
            model=None,
            auto_open_conversation=False,
        )


@respx.mock
def test_run_smart_routing_launches_a_pinned_codex_bare(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    # `run --harness codex-native --smart-routing` with no -p is servable.
    _mock_info()
    _mock_hosts({"codex-native": True})
    route = _mock_create(harness="codex-native")
    captured: dict[str, Any] = {}
    monkeypatch.setattr("omnigent.codex_native.run_codex_native", lambda **kw: captured.update(kw))

    _run_smart_routing(**_run_kwargs(harness="codex-native", prompt=None))

    assert "smart_routing_message" not in json.loads(route.calls.last.request.content)
    assert captured["session_id"] == _SESSION_ID
    assert captured["model"] is None
    assert captured["prompt"] is None


@respx.mock
def test_dispatch_fails_open_to_a_fresh_wrapper_session(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rejected create still launches — the wrapper bundles its own session."""
    _mock_info()
    _mock_hosts(None)
    respx.post(f"{_BASE}/v1/sessions").mock(return_value=httpx.Response(503, text="down"))
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native", lambda **kw: captured.update(kw)
    )

    _dispatch_smart_routing(
        harness=None,
        server=None,
        prompt="port the parser",
        model=None,
        auto_open_conversation=False,
    )

    err = capsys.readouterr().err
    assert "Smart Routing was unavailable" in err
    assert "launching claude-native" in err
    assert captured["session_id"] is None
    assert captured["extra_args"] == ()
    assert captured["prompt"] == "port the parser"


@respx.mock
def test_dispatch_tier3_falls_back_when_the_pick_cannot_take_a_prompt(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A harness the CLI cannot hand a prompt to is not a usable pick."""
    _mock_info()
    _mock_hosts(None)
    _mock_create(harness="cursor-native", model_override="composer-2.5")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native", lambda **kw: captured.update(kw)
    )

    _dispatch_smart_routing(
        harness=None,
        server=None,
        prompt="port the parser",
        model=None,
        auto_open_conversation=False,
    )

    assert "did not resolve a launchable harness" in capsys.readouterr().err
    assert captured["extra_args"] == ("--model", "composer-2.5")


@respx.mock
def test_dispatch_preflight_error_blocks_the_launch(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    """An unavailable-routing error must stop before any wrapper runs."""
    _mock_info(enabled=False)
    _mock_hosts(None)

    def _must_not_launch(**_kwargs: Any) -> None:
        raise AssertionError("wrapper launched despite unavailable routing")

    monkeypatch.setattr("omnigent.claude_native.run_claude_native", _must_not_launch)

    with pytest.raises(ClickException, match="Smart Routing is not enabled"):
        _dispatch_smart_routing(
            harness="claude-native",
            server=None,
            prompt="hello",
            model=None,
            auto_open_conversation=False,
        )


@respx.mock
def test_dispatch_routes_hostlessly_when_the_server_does_not_know_the_host(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    """An unregistered host degrades to a hostless route, not a failed create."""
    _mock_info()
    respx.get(f"{_BASE}/v1/hosts").mock(return_value=httpx.Response(200, json={"hosts": []}))
    route = _mock_create(harness="claude-native", model_override="claude-sonnet-5")
    monkeypatch.setattr("omnigent.claude_native.run_claude_native", lambda **kw: None)

    _dispatch_smart_routing(
        harness="claude-native",
        server=None,
        prompt="hello",
        model=None,
        auto_open_conversation=False,
    )

    payload = json.loads(route.calls.last.request.content)
    assert "host_id" not in payload
    assert "workspace" not in payload


def test_known_host_id_requires_the_server_to_have_seen_the_host() -> None:
    with respx.mock:
        _mock_hosts(None)
        assert known_host_id(base_url=_BASE, host_id=_HOST_ID) == _HOST_ID
        assert known_host_id(base_url=_BASE, host_id="host_other") is None
        assert known_host_id(base_url=_BASE, host_id=None) is None


# ── flag parsing / rejects ───────────────────────────────────────────────


def test_require_prompt_rejects_missing_text_without_an_in_harness_route() -> None:
    for value in (None, "   "):
        with pytest.raises(UsageError, match="needs the text to route"):
            _require_smart_routing_prompt(value, in_harness_routing=False)
    assert _require_smart_routing_prompt("hi", in_harness_routing=False) == "hi"


def test_require_prompt_allows_missing_text_when_the_harness_routes_its_own() -> None:
    # The per-harness subcommands default to the in-harness route, so a bare
    # launch is served instead of rejected.
    assert _require_smart_routing_prompt(None) is None
    assert _require_smart_routing_prompt("   ") is None
    assert _require_smart_routing_prompt("hi") == "hi"


def _run_kwargs(**overrides: Any) -> dict[str, Any]:
    """``_run_smart_routing`` kwargs with a valid routed launch as the baseline."""
    base: dict[str, Any] = {
        "target": None,
        "harness": None,
        "prompt": "hello",
        "server": None,
        "model": None,
        "resume_conversation_id": None,
        "resume_picker": False,
        "resume_latest": False,
        "auto_open_conversation": False,
    }
    base.update(overrides)
    return base


def test_run_smart_routing_rejects_an_agent() -> None:
    with pytest.raises(ClickException, match="takes no AGENT"):
        _run_smart_routing(**_run_kwargs(target="examples/hello_world.yaml"))


@pytest.mark.parametrize(
    ("overrides", "flag"),
    [
        ({"tools": "coding"}, "--tools"),
        ({"system_prompt": "be terse"}, "--system-prompt"),
        ({"log": True}, "--log"),
        ({"debug_events": True}, "--debug-events"),
        ({"fork_session_id": "conv_src"}, "--fork"),
        ({"ephemeral": True}, "--no-session"),
    ],
)
def test_run_smart_routing_rejects_repl_only_options(overrides: dict[str, Any], flag: str) -> None:
    """A routed launch is still a TUI attach — REPL-only flags fail loud."""
    with pytest.raises(ClickException, match=flag):
        _run_smart_routing(**_run_kwargs(**overrides))


@pytest.mark.parametrize(
    ("overrides", "flag"),
    [
        ({"resume_conversation_id": "conv_old"}, "--resume"),
        ({"resume_picker": True}, "--resume"),
        ({"resume_latest": True}, "--continue"),
    ],
)
def test_run_smart_routing_rejects_resuming(overrides: dict[str, Any], flag: str) -> None:
    """Routing happens at create, so a routed launch is always a new session."""
    with pytest.raises(ClickException, match=f"cannot be combined with {flag}"):
        _run_smart_routing(**_run_kwargs(**overrides))


def test_run_smart_routing_rejects_a_non_routable_harness() -> None:
    with pytest.raises(ClickException, match="does not support --harness 'claude-sdk'"):
        _run_smart_routing(**_run_kwargs(harness="claude-sdk"))


@pytest.mark.parametrize(
    "args",
    [
        ["run", "--smart-routing"],
        ["run", "--smart-routing", "--harness", "auto"],
    ],
)
def test_cross_harness_routing_without_a_prompt_is_a_usage_error(args: list[str]) -> None:
    # Nothing exists to hook before the harness is picked, so -p is required.
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 2, result.output
    assert "needs the text to route up front here" in result.output
    assert "no harness that can route from inside itself" in result.output
    assert '-p "<prompt>"' in result.output
    assert "--harness claude-native" in result.output


def test_a_harness_without_the_first_message_hook_still_needs_a_prompt() -> None:
    # kiro-native is routable, but it cannot route from inside itself.
    with pytest.raises(UsageError, match="needs the text to route up front here"):
        _run_smart_routing(**_run_kwargs(harness="kiro-native", prompt=None))


@pytest.mark.parametrize(
    "args",
    [
        ["run", "--smart-routing", "-p", "hi", "--continue"],
        ["claude", "--smart-routing", "-p", "hi", "--resume", "conv_old"],
        ["codex", "--smart-routing", "-p", "hi", "--resume", "conv_old"],
        ["claude", "--smart-routing", "-p", "hi", "--session", "conv_old"],
    ],
)
def test_smart_routing_with_a_resume_is_rejected(args: list[str]) -> None:
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 1, result.output
    assert "routes a new session" in result.output


@respx.mock
def test_run_smart_routing_with_a_pinned_harness_routes_the_model_only(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    """``run --harness codex-native --smart-routing`` keeps codex, routes model."""
    _mock_info()
    _mock_hosts({"codex-native": True})
    route = _mock_create(harness="codex-native", model_override="gpt-5.4-mini")
    captured: dict[str, Any] = {}
    monkeypatch.setattr("omnigent.codex_native.run_codex_native", lambda **kw: captured.update(kw))

    _run_smart_routing(**_run_kwargs(harness="codex-native", prompt="add a --dry-run flag"))

    assert "harness_override" not in json.loads(route.calls.last.request.content)
    assert captured["model"] == "gpt-5.4-mini"
    assert captured["session_id"] == _SESSION_ID


def test_routable_harnesses_are_the_prompt_capable_native_ones() -> None:
    assert _smart_routing_capable_harness("claude-native") == "claude-native"
    assert _smart_routing_capable_harness("codex-native") == "codex-native"
    assert _smart_routing_capable_harness("kiro-native") == "kiro-native"
    # Wrappers with no prompt parameter, and non-native harnesses, are out —
    # including bare ``claude``, which canonicalizes to the SDK harness.
    assert _smart_routing_capable_harness("cursor-native") is None
    assert _smart_routing_capable_harness("claude") is None
    assert _smart_routing_capable_harness("claude-sdk") is None
    assert _smart_routing_capable_harness(None) is None


@pytest.mark.parametrize(
    ("args", "model", "expected"),
    [
        ((), "sonnet", ("--model", "sonnet")),
        (("--verbose",), "sonnet", ("--verbose", "--model", "sonnet")),
        # An explicit user model wins over the routed default.
        (("--model", "opus"), "sonnet", ("--model", "opus")),
        (("--model=opus",), "sonnet", ("--model=opus",)),
        (("--verbose",), None, ("--verbose",)),
    ],
)
def test_routed_model_arg_merge(
    args: tuple[str, ...], model: str | None, expected: tuple[str, ...]
) -> None:
    assert _with_routed_model_arg(args, model) == expected


# ── the dedicated subcommands ────────────────────────────────────────────


@respx.mock
def test_claude_subcommand_attaches_to_the_routed_session(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    """``omnigent claude --smart-routing -p`` routes, then attaches with --model."""
    _mock_info()
    _mock_hosts({"claude-native": True})
    _mock_create(harness="claude-native", model_override="claude-sonnet-5")
    captured: dict[str, Any] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr(
        "omnigent.claude_native.run_claude_native", lambda **kw: captured.update(kw)
    )

    result = CliRunner().invoke(cli, ["claude", "--smart-routing", "-p", "fix the flaky test"])

    assert result.exit_code == 0, result.output
    assert captured["session_id"] == _SESSION_ID
    assert captured["extra_args"] == ("--model", "claude-sonnet-5")
    assert captured["prompt"] == "fix the flaky test"


@respx.mock
def test_codex_subcommand_attaches_to_the_routed_session(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    """Codex takes the routed model first-class, and keeps its own -p delivery."""
    _mock_info()
    _mock_hosts({"codex-native": True})
    _mock_create(harness="codex-native", model_override="gpt-5.4-mini")
    captured: dict[str, Any] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.codex_native.run_codex_native", lambda **kw: captured.update(kw))

    result = CliRunner().invoke(cli, ["codex", "--smart-routing", "-p", "port the parser"])

    assert result.exit_code == 0, result.output
    assert captured["session_id"] == _SESSION_ID
    assert captured["model"] == "gpt-5.4-mini"
    assert captured["prompt"] == "port the parser"


@respx.mock
@pytest.mark.parametrize(
    ("command", "launcher"),
    [
        ("claude", "omnigent.claude_native.run_claude_native"),
        ("codex", "omnigent.codex_native.run_codex_native"),
    ],
)
def test_subcommand_without_a_prompt_arms_the_session_and_launches_bare(
    command: str, launcher: str, monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    # `omnigent claude|codex --smart-routing` needs no -p: the hook routes.
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
    assert captured["session_id"] == _SESSION_ID
    assert captured["prompt"] is None
    # No create-time pick, so no routed model reaches the wrapper.
    assert captured["extra_args"] == ()
    assert captured.get("model") is None
    assert "Smart Routing is on for this session" in result.output


@respx.mock
def test_explicit_model_beats_the_routed_pick(
    monkeypatch: pytest.MonkeyPatch, _routing_env: None
) -> None:
    """Routing fills in a default; a model the user typed still wins."""
    _mock_info()
    _mock_hosts(None)
    _mock_create(harness="codex-native", model_override="gpt-5.4-mini")
    captured: dict[str, Any] = {}
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.codex_native.run_codex_native", lambda **kw: captured.update(kw))

    result = CliRunner().invoke(
        cli, ["codex", "--smart-routing", "-p", "hello", "--model", "gpt-5.4"]
    )

    assert result.exit_code == 0, result.output
    assert captured["model"] == "gpt-5.4"


@respx.mock
def test_claude_subcommand_falls_back_to_a_fresh_session(
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

    result = CliRunner().invoke(cli, ["claude", "--smart-routing", "-p", "hello"])

    assert result.exit_code == 0, result.output
    assert captured["session_id"] is None
    assert captured["prompt"] == "hello"
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

    result = CliRunner().invoke(cli, ["claude", "--smart-routing", "-p", "hello"])

    # Not fatal.
    assert result.exit_code == 0, result.output
    # One explicit line, naming the status the server answered with.
    assert "Smart Routing was unavailable" in result.output
    assert "400" in result.output
    # And the session still launches, unrouted and prompt in hand.
    assert captured["session_id"] is None
    assert captured["prompt"] == "hello"
    assert captured["extra_args"] == ()


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


# ── credential-provider gating, end to end ───────────────────────────────
#
# The shape a Databricks user actually has: Claude Code pointed at the workspace
# AI Gateway, Codex signed in to a personal ChatGPT subscription. Routing is
# applied by rewriting the launch model to a gateway catalog id, so only the
# Claude pane can honour it — every routed entry point into Codex must fail
# fast, and the auto route (which may land on either arm) with it.

_CODEX_ON_SUBSCRIPTION = {"claude-native": True, "codex-native": False}
_NO_GATEWAY_AT_ALL = {"claude-native": False, "codex-native": False}


@respx.mock
def test_auto_route_is_refused_when_only_one_arm_is_gateway_backed() -> None:
    _mock_info()
    _mock_hosts(_CODEX_ON_SUBSCRIPTION)

    with pytest.raises(ClickException, match=r"codex-native.*not AI-Gateway-backed"):
        check_smart_routing_available(
            base_url=_BASE,
            harnesses=smart_routing_families(None),
            host_id=_HOST_ID,
        )


@respx.mock
def test_claude_route_survives_codex_being_on_a_subscription() -> None:
    """Per-family gating: the arm that IS on the gateway keeps its routing."""
    _mock_info()
    _mock_hosts(_CODEX_ON_SUBSCRIPTION)

    check_smart_routing_available(
        base_url=_BASE,
        harnesses=smart_routing_families("claude-native"),
        host_id=_HOST_ID,
    )


@respx.mock
@pytest.mark.parametrize(
    ("args", "gateway"),
    [
        # The auto route may land on Codex, so one ungatewayed arm blocks it.
        (["run", "--smart-routing", "-p", "hello"], _CODEX_ON_SUBSCRIPTION),
        (
            ["run", "--harness", "codex-native", "--smart-routing", "-p", "hello"],
            _CODEX_ON_SUBSCRIPTION,
        ),
        (["codex", "--smart-routing", "-p", "hello"], _CODEX_ON_SUBSCRIPTION),
        # No AI Gateway anywhere: every entry point errors, Claude included.
        (["run", "--smart-routing", "-p", "hello"], _NO_GATEWAY_AT_ALL),
        (["claude", "--smart-routing", "-p", "hello"], _NO_GATEWAY_AT_ALL),
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
