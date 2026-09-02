"""Launch-site behaviour for the per-session subagent-routing endpoint."""

from __future__ import annotations

import asyncio
import http.client
import stat
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.codex_executor import CODEX_EXTENDED_CATALOG_ENV_VAR
from omnigent.inner.hook_scripts.subagent_router import read_router_endpoint
from omnigent.runner import subagent_routing
from omnigent.runner.app import _build_spawn_env_from_spec, _ensure_session_subagent_router
from omnigent.runner.native.orchestration import _start_subagent_router_for_native_session
from omnigent.runner.subagent_routing import SessionRoutingClass
from omnigent.spec.types import AgentSpec, ExecutorSpec

# The three session classes the codex launch paths distinguish.
_PLAIN = SessionRoutingClass()
_PINNED = SessionRoutingClass(routing_enabled=True)
_AUTO = SessionRoutingClass(routing_enabled=True, auto_harness=True)


class _DeadClient:
    """Stands in for the runner→server client; never actually called."""

    async def post(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[explicit-any]
        raise RuntimeError("server down")


@pytest.fixture(autouse=True)
def _cleanup_routers() -> Any:  # type: ignore[explicit-any]
    yield
    for session_id in ("conv_native_launch", "conv_sdk_launch"):
        subagent_routing.shutdown_session_router(session_id)


@pytest.mark.parametrize("harness", ["claude-native", "codex-native"])
@pytest.mark.parametrize("routing_class", [_PINNED, _AUTO])
async def test_a_routed_native_launch_installs_the_router(
    tmp_path: Path, harness: str, routing_class: SessionRoutingClass
) -> None:
    """Either family routes spawns whether or not the harness is auto-picked.

    On codex the advertisement is also what turns on the generated
    ``hooks.json`` spawn gate and the routed-spawn tool pre-approvals, so
    withholding it from a pinned Smart Routing session left that session unable
    to spawn at all — not merely unrouted.
    """
    advertised, router = _start_subagent_router_for_native_session(
        "conv_native_launch",
        bridge_dir=tmp_path,
        harness=harness,
        server_client=_DeadClient(),  # type: ignore[arg-type]
        routing_enabled=routing_class.routing_enabled,
        auto_harness=routing_class.auto_harness,
    )
    assert advertised == tmp_path
    assert router is not None
    assert read_router_endpoint(tmp_path) is not None


@pytest.mark.parametrize("harness", ["claude-native", "codex-native"])
async def test_a_plain_native_launch_gets_no_router(tmp_path: Path, harness: str) -> None:
    """A plain session pays none of routing's cost.

    No loopback server, no bearer token on disk, and (because the hooks are
    pointed at the advertisement that is never written) no ``Task`` /
    ``spawn_agent`` PreToolUse hook stalling every spawn on a verdict the
    server would never route anyway.
    """
    assert _start_subagent_router_for_native_session(
        "conv_native_launch",
        bridge_dir=tmp_path,
        harness=harness,
        server_client=_DeadClient(),  # type: ignore[arg-type]
        routing_enabled=False,
        auto_harness=False,
    ) == (None, None)
    assert read_router_endpoint(tmp_path) is None


async def test_a_pinned_codex_sdk_session_gets_no_router(tmp_path: Path) -> None:
    """The SDK codex arm keeps the auto-harness requirement.

    Its spawns go through the session-create path, which already routes off the
    stamped switch, so an in-harness ``spawn_agent`` gate would only add a
    round trip — and with it the generated ``hooks.json`` that stops the user's
    own hooks file from being symlinked through.
    """
    assert _start_subagent_router_for_native_session(
        "conv_native_launch",
        bridge_dir=tmp_path,
        harness="codex",
        server_client=_DeadClient(),  # type: ignore[arg-type]
        routing_enabled=True,
        auto_harness=False,
    ) == (None, None)
    assert read_router_endpoint(tmp_path) is None


async def test_native_launch_skips_without_a_server_client(tmp_path: Path) -> None:
    assert _start_subagent_router_for_native_session(
        "conv_native_launch",
        bridge_dir=tmp_path,
        harness="claude-native",
        server_client=None,
        routing_enabled=True,
        auto_harness=True,
    ) == (None, None)


async def test_stale_handle_shutdown_leaves_a_relaunched_router_alive(tmp_path: Path) -> None:
    """A forwarder's late ``finally`` must not kill the re-created router."""
    session_id = "conv_native_launch"
    _, first = _start_subagent_router_for_native_session(
        session_id,
        bridge_dir=tmp_path,
        harness="claude-native",
        server_client=_DeadClient(),  # type: ignore[arg-type]
        routing_enabled=True,
        auto_harness=True,
    )
    assert first is not None
    # Terminal re-create: the old router goes away and a new one binds.
    subagent_routing.shutdown_session_router(session_id, first)
    _, second = _start_subagent_router_for_native_session(
        session_id,
        bridge_dir=tmp_path,
        harness="claude-native",
        server_client=_DeadClient(),  # type: ignore[arg-type]
        routing_enabled=True,
        auto_harness=True,
    )
    assert second is not None and second is not first

    # The old forwarder's delayed teardown fires now.
    subagent_routing.shutdown_session_router(session_id, first)

    assert subagent_routing._session_routers.get(session_id) is second
    assert not second._closed
    advertised = read_router_endpoint(tmp_path)
    assert advertised is not None
    assert advertised.url == second.url


async def test_unscoped_shutdown_still_tears_down_the_live_router(tmp_path: Path) -> None:
    session_id = "conv_native_launch"
    _, router = _start_subagent_router_for_native_session(
        session_id,
        bridge_dir=tmp_path,
        harness="claude-native",
        server_client=_DeadClient(),  # type: ignore[arg-type]
        routing_enabled=True,
        auto_harness=True,
    )
    assert router is not None
    subagent_routing.shutdown_session_router(session_id)
    assert router._closed
    assert session_id not in subagent_routing._session_routers


@pytest.mark.parametrize("routing_class", [_PINNED, _AUTO])
async def test_a_routed_claude_sdk_launch_installs_the_router(
    routing_class: SessionRoutingClass,
) -> None:
    await _ensure_session_subagent_router(
        "conv_sdk_launch",
        "claude-sdk",
        server_client=_DeadClient(),  # type: ignore[arg-type]
        routing_class=routing_class,
    )
    env = subagent_routing.session_router_env("conv_sdk_launch", "claude-sdk")
    assert env["OMNIGENT_SUBAGENT_ROUTER_SESSION_ID"] == "conv_sdk_launch"


async def test_a_plain_claude_sdk_launch_gets_no_router() -> None:
    """No router means no env, so the executor registers no ``Task`` hook."""
    await _ensure_session_subagent_router(
        "conv_sdk_launch",
        "claude-sdk",
        server_client=_DeadClient(),  # type: ignore[arg-type]
        routing_class=_PLAIN,
    )
    assert "conv_sdk_launch" not in subagent_routing._session_routers
    assert subagent_routing.session_router_env("conv_sdk_launch", "claude-sdk") == {}


@pytest.mark.parametrize("routing_class", [_PLAIN, _PINNED])
async def test_codex_sdk_launch_skips_the_router_unless_auto_harness(
    routing_class: SessionRoutingClass,
) -> None:
    await _ensure_session_subagent_router(
        "conv_sdk_launch",
        "codex",
        server_client=_DeadClient(),  # type: ignore[arg-type]
        routing_class=routing_class,
    )
    assert "conv_sdk_launch" not in subagent_routing._session_routers
    assert subagent_routing.session_router_env("conv_sdk_launch", "codex") == {}


async def test_codex_sdk_launch_installs_the_router_for_an_auto_harness_session() -> None:
    await _ensure_session_subagent_router(
        "conv_sdk_launch",
        "codex",
        server_client=_DeadClient(),  # type: ignore[arg-type]
        routing_class=_AUTO,
    )
    env = subagent_routing.session_router_env("conv_sdk_launch", "codex")
    assert env["OMNIGENT_CODEX_SUBAGENT_ROUTER_SESSION_ID"] == "conv_sdk_launch"


async def test_the_routing_class_falls_back_to_what_session_init_stamped() -> None:
    """The env is rebuilt on every respawn, long after the init envelope."""
    subagent_routing.remember_session_routing_class("conv_sdk_launch", _AUTO)
    try:
        await _ensure_session_subagent_router(
            "conv_sdk_launch",
            "codex",
            server_client=_DeadClient(),  # type: ignore[arg-type]
        )
        assert "conv_sdk_launch" in subagent_routing._session_routers
    finally:
        subagent_routing.forget_session_routing_class("conv_sdk_launch")


async def test_an_unknown_session_reads_as_plain() -> None:
    assert subagent_routing.session_routing_class("conv_never_seen") == _PLAIN
    await _ensure_session_subagent_router(
        "conv_sdk_launch",
        "codex",
        server_client=_DeadClient(),  # type: ignore[arg-type]
    )
    assert "conv_sdk_launch" not in subagent_routing._session_routers


@pytest.mark.parametrize(
    ("routing_class", "expected"),
    [
        # A plain codex session keeps codex's bundled catalog, so it never pays
        # the ~10 s ``codex debug models`` probe at boot.
        (_PLAIN, None),
        # A pinned Smart Routing session's routed turn can land on a gateway arm
        # that catalog has no entry for, which codex then refuses client-side.
        (_PINNED, "1"),
        (_AUTO, "1"),
    ],
)
def test_the_codex_spawn_env_carries_the_catalog_flag_per_session_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routing_class: SessionRoutingClass,
    expected: str | None,
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    (tmp_path / "config.yaml").write_text(
        "providers:\n"
        "  openai:\n"
        "    kind: key\n"
        "    default: true\n"
        "    openai:\n"
        "      base_url: https://api.openai.com/v1\n"
        "      api_key: $OPENAI_API_KEY\n"
        "      models:\n"
        "        default: gpt-5-4\n"
    )
    spec = AgentSpec(
        spec_version=1,
        name="x",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex"}),
    )
    subagent_routing.remember_session_routing_class("conv_spawn_env", routing_class)
    try:
        env = _build_spawn_env_from_spec(spec, "codex", session_id="conv_spawn_env")
    finally:
        subagent_routing.forget_session_routing_class("conv_spawn_env")

    assert env is not None
    assert env.get(CODEX_EXTENDED_CATALOG_ENV_VAR) == expected


def test_the_claude_spawn_env_never_carries_the_codex_catalog_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    (tmp_path / "config.yaml").write_text(
        "providers:\n"
        "  anthropic:\n"
        "    kind: key\n"
        "    default: true\n"
        "    anthropic:\n"
        "      base_url: https://api.anthropic.com\n"
        "      api_key: $ANTHROPIC_API_KEY\n"
        "      models:\n"
        "        default: test-default\n"
    )
    spec = AgentSpec(
        spec_version=1,
        name="x",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
    )
    subagent_routing.remember_session_routing_class("conv_spawn_env", _AUTO)
    try:
        env = _build_spawn_env_from_spec(spec, "claude-sdk", session_id="conv_spawn_env")
    finally:
        subagent_routing.forget_session_routing_class("conv_spawn_env")

    assert env is not None
    assert CODEX_EXTENDED_CATALOG_ENV_VAR not in env


async def test_router_env_is_scoped_to_the_launching_harness(tmp_path: Path) -> None:
    """A codex spawn beneath a claude session must not see the codex vars.

    They would carry the parent claude session's id, so the codex executor
    would route and audit as the wrong session.
    """
    claude_env = subagent_routing.router_env("conv_x", tmp_path, harness="claude-sdk")
    codex_env = subagent_routing.router_env("conv_x", tmp_path, harness="codex")
    assert set(claude_env) == {
        "OMNIGENT_SUBAGENT_ROUTER_DIR",
        "OMNIGENT_SUBAGENT_ROUTER_SESSION_ID",
    }
    assert set(codex_env) == {
        "OMNIGENT_CODEX_SUBAGENT_ROUTER_DIR",
        "OMNIGENT_CODEX_SUBAGENT_ROUTER_SESSION_ID",
    }
    # A harness with no routing hooks gets nothing at all.
    assert subagent_routing.router_env("conv_x", tmp_path, harness="pi") == {}


async def test_sdk_launch_survives_an_unusable_router_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A poisoned bridge root must not fail session creation."""
    from omnigent.runner import subagent_routing as routing_mod

    def _boom(session_id: str) -> Path:
        raise RuntimeError("unsafe bridge root")

    monkeypatch.setattr(routing_mod, "router_dir_for_session", _boom)
    await _ensure_session_subagent_router(
        "conv_sdk_launch",
        "claude-sdk",
        server_client=_DeadClient(),  # type: ignore[arg-type]
        routing_class=_AUTO,
    )
    assert subagent_routing.session_router_env("conv_sdk_launch", "claude-sdk") == {}


@pytest.mark.parametrize("harness", ["pi", "copilot", "goose"])
async def test_sdk_launch_skips_harnesses_without_spawn_hooks(harness: str) -> None:
    """No hook reads the advertisement, so no endpoint is started at all."""
    await _ensure_session_subagent_router(
        "conv_sdk_launch",
        harness,
        server_client=_DeadClient(),  # type: ignore[arg-type]
        routing_class=_AUTO,
    )
    assert "conv_sdk_launch" not in subagent_routing._session_routers


async def test_sdk_launch_skips_native_harnesses() -> None:
    await _ensure_session_subagent_router(
        "conv_sdk_launch",
        "claude-native",
        server_client=_DeadClient(),  # type: ignore[arg-type]
        routing_class=_AUTO,
    )
    assert subagent_routing.session_router_env("conv_sdk_launch", "claude-native") == {}


# ── Endpoint hardening ──────────────────────────────────────────────


async def test_non_ascii_authorization_header_is_rejected_not_crashed(tmp_path: Path) -> None:
    """``compare_digest`` raises TypeError on a non-ASCII str operand."""

    async def _resolver(session_id: str, req: Any) -> Any:  # type: ignore[explicit-any]
        raise AssertionError("resolver must not run for an unauthorized request")

    router = subagent_routing.start_subagent_router(
        bridge_dir=tmp_path,
        session_id="conv_auth",
        resolver=_resolver,
        loop=asyncio.get_running_loop(),
    )
    try:
        host, port = router.httpd.server_address[0], router.httpd.server_address[1]

        def _post() -> int:
            conn = http.client.HTTPConnection(str(host), int(port), timeout=10)
            try:
                conn.request(
                    "POST",
                    "/v1/sessions/conv_auth/route-subagent",
                    body=b"{}",
                    headers={"Authorization": "Bearer \u00fc\u00e9"},
                )
                return conn.getresponse().status
            finally:
                conn.close()

        assert await asyncio.to_thread(_post) == 401
    finally:
        router.close()


def test_advertisement_is_written_owner_only_with_no_leftover_temp(tmp_path: Path) -> None:
    path = subagent_routing.write_advertisement(
        tmp_path, url="http://127.0.0.1:1", token="tok", session_id="conv_x"
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # A unique temp name is used, so nothing may survive the rename.
    assert [p.name for p in tmp_path.iterdir()] == [path.name]
    subagent_routing.write_advertisement(tmp_path, url="http://127.0.0.1:2", token="tok2")
    assert [p.name for p in tmp_path.iterdir()] == [path.name]


def test_prune_never_removes_the_shared_router_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent import claude_native_bridge

    root = tmp_path / "subagent-routers"
    root.mkdir()
    monkeypatch.setattr(claude_native_bridge, "subagent_router_bridge_root", lambda: root)
    router = subagent_routing.SubagentRouter(
        bridge_dir=root,
        url="http://127.0.0.1:1",
        token="tok",
        httpd=None,  # type: ignore[arg-type]
    )
    subagent_routing._prune_router_dirs(router)
    assert root.is_dir()
