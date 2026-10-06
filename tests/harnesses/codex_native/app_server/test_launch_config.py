"""Launch config tests for Codex app server."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]
from omnigent.harnesses.codex_native.app_server import (
    build_codex_native_server,
    codex_terminal_env,
)
from tests.harnesses.codex_native.app_server._support import (
    _disable_codex_startup_rpc,
    _test_app_server,
)


def test_build_codex_native_server_profile_error_names_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Missing Databricks profile errors identify the runner-visible profile.

    The native Codex terminal can fail before the TUI launches if the
    runner process cannot resolve the Databricks profile it was given.
    The message must include that profile name so operators can tell a
    stale/missing runner env apart from a generic Codex startup failure.
    """
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._find_codex_cli",
        lambda: sys.executable,
    )
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._databricks_gateway_host",
        lambda _profile: None,
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "missing-databrickscfg"))

    with pytest.raises(OSError, match="profile 'oss'"):
        build_codex_native_server(
            socket_path=tmp_path / "codex.sock",
            codex_home=tmp_path / "codex-home",
            cwd=tmp_path,
            model=None,
            profile="oss",
            bridge_dir=tmp_path / "bridge",
            ap_server_url=None,
            ap_auth_headers={},
        )


def test_build_codex_native_server_uses_profile_host_without_static_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Native Codex accepts Databricks CLI OAuth profiles without static tokens.

    A default Omnigent install may not include ``databricks-sdk`` in the
    runner process. In that case a bearer cannot be minted at startup, but the
    profile's host is still enough: Codex gets an ``auth.command`` that runs
    ``databricks auth token --profile`` at request time.
    """
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._find_codex_cli",
        lambda: sys.executable,
    )
    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text(
        "\n".join(
            [
                "[oss]",
                "host = https://example.cloud.databricks.com",
                "auth_type = databricks-cli",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))

    # This test exercises the profile-host base URL + auth command, not model
    # resolution. Force live codex discovery offline so the build makes no
    # model-services network call for the profile host; the explicit
    # ``model="test-model"`` is then used as-is.
    def _discovery_offline(_profile: str | None) -> object:
        raise RuntimeError("model discovery is offline in this test")

    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        _discovery_offline,
    )

    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model="test-model",
        profile="oss",
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
    )

    overrides = "\n".join(app_server.config_overrides)
    assert "https://example.cloud.databricks.com/ai-gateway/codex/v1" in overrides
    assert 'databricks auth token --profile \\"oss\\"' in overrides


def test_native_codex_resource_attributes_reach_server_and_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment=example,launch_mode=direct")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer test-token")
    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=None,
        profile=None,
        bridge_dir=tmp_path / "bridge",
        codex_path=sys.executable,
    )

    for env in (app_server.env, codex_terminal_env(app_server)):
        assert {key: value for key, value in env.items() if key.startswith("OTEL_")} == {
            "OTEL_RESOURCE_ATTRIBUTES": "deployment=example,launch_mode=omni"
        }


def test_build_codex_native_server_without_bypass_emits_no_bypass_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The default (``bypass_sandbox=False``) writes no approval/sandbox overrides.

    Guards the safe default: an app-server built without the opt-in must
    leave Codex's normal approval-prompt + own-sandbox stance untouched, so
    no ``approval_policy`` / ``sandbox_mode`` override leaks in. A regression
    that always emitted them would silently disable the sandbox for every
    native Codex session.
    """
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._find_codex_cli",
        lambda: sys.executable,
    )
    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=None,
        profile=None,
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
    )

    overrides = "\n".join(app_server.config_overrides)
    assert "approval_policy" not in overrides
    assert "sandbox_mode" not in overrides


def test_build_codex_native_server_bypass_emits_full_access_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``bypass_sandbox=True`` puts the app-server threads into the bypass stance.

    The ``--remote`` TUI launched with
    ``--dangerously-bypass-approvals-and-sandbox`` fixes the thread's
    approval/sandbox stance, but the chat/forwarder seam drives the SAME
    thread through the app-server, so the app-server config must match —
    ``approval_policy="never"`` (no prompts a headless seam can't answer)
    and ``sandbox_mode="danger-full-access"`` (commands run with no command
    sandbox, the #657 ask). Without these the app-server-driven turns would
    keep prompting / keep the sandbox even though the TUI bypassed it.
    """
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._find_codex_cli",
        lambda: sys.executable,
    )
    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=None,
        profile=None,
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
        bypass_sandbox=True,
    )

    assert 'approval_policy="never"' in app_server.config_overrides
    assert 'sandbox_mode="danger-full-access"' in app_server.config_overrides


@pytest.mark.parametrize(
    ("model", "expected_pin"),
    [
        pytest.param(None, "gpt-5.4-mini", id="default-launch"),
        pytest.param("gpt-5.5", "gpt-5.5", id="explicit-pick"),
    ],
)
def test_build_codex_native_server_pins_profile_resolved_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str | None,
    expected_pin: str,
) -> None:
    """
    A profile launch pins the model it routes to, in codex's own spelling.

    A launch naming no model still gets a concrete model from the profile's
    catalog via ``-c model=``, which outranks the ``config.toml`` copied from
    the user's shared home. Leaving ``pinned_model`` unset there let the
    forwarder mirror the shared file's stale model back as this session's
    ``model_override`` (live-caught: a session running
    ``databricks-gpt-5-6-luna`` reported the shared file's ``gpt-5.4``).
    The pin uses codex's spelling because that is the vocabulary
    ``config.toml`` and every reader of it — including the web catalog's
    row ids — compare in.
    """
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._find_codex_cli",
        lambda: sys.executable,
    )
    monkeypatch.setattr(
        codex_native_app_server,
        "_databricks_launch_materialization",
        lambda *, model, profile, codex_path: (
            codex_native_app_server._DatabricksLaunchMaterialization(
                config_overrides=[f'model="{model or "databricks-gpt-5-4-mini"}"'],
                model=model or "databricks-gpt-5-4-mini",
                host="https://ws.example",
            )
        ),
    )

    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=model,
        profile="oss",
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
    )

    assert app_server.pinned_model == expected_pin


@pytest.mark.parametrize(
    ("model", "profile", "extra_overrides"),
    [
        # Subscription: an explicit pick and a catalog-resolved Default.
        ("gpt-5.5", None, ['model_provider="openai"']),
        ("gpt-5.6-terra", None, ['model_provider="openai"']),
        # cli-config: the user's own provider table rides the config copy.
        ("gpt-5.5", None, ['model_provider="Databricks"']),
        # Databricks profile: Default and an explicit pick.
        (None, "oss", None),
        ("databricks-gpt-5-5", "oss", None),
    ],
)
def test_launch_argv_and_config_pin_name_the_same_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str | None,
    profile: str | None,
    extra_overrides: list[str] | None,
) -> None:
    """
    The argv ``-c model=`` and the config copy's pin never drift apart.

    Both artifacts are written from one resolved value, on EVERY provider
    shape and for Default and explicit picks alike — the structural end of
    the stale-config-line class where a copied ``model =`` governed a
    session the argv never named. On the profile shape the file
    deliberately holds codex's own spelling (the vocabulary its readers
    compare in) while argv carries the wire spelling; the guard asserts
    they name the same model, and byte-identity everywhere else.
    """
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.models.codex_model_vocabulary import comparable_model_id

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._find_codex_cli",
        lambda: sys.executable,
    )
    monkeypatch.setattr(
        codex_native_app_server,
        "_databricks_launch_materialization",
        lambda *, model, profile, codex_path: (
            codex_native_app_server._DatabricksLaunchMaterialization(
                config_overrides=[f'model="{model or "databricks-gpt-5-6-luna"}"'],
                model=model or "databricks-gpt-5-6-luna",
                host="https://ws.example",
            )
        ),
    )

    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=model,
        profile=profile,
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
        extra_config_overrides=list(extra_overrides) if extra_overrides else None,
    )

    argv_models = [
        override.split("=", 1)[1]
        for override in app_server.config_overrides
        if override.split("=", 1)[0] == "model"
    ]
    assert len(argv_models) == 1, app_server.config_overrides
    argv_model = json.loads(argv_models[0]) if argv_models[0].startswith('"') else argv_models[0]
    pinned = app_server.pinned_model
    assert pinned, "every launch must pin a model"
    assert comparable_model_id(argv_model) == comparable_model_id(pinned)
    if profile is None:
        assert argv_model == pinned


async def test_start_pins_reasoning_effort_in_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Startup seeds ``model_reasoning_effort`` from the session's persisted effort.

    The private config is a copy of the user's shared one, whose effort line is
    whatever the user last ran. Both the app-server and the ``--remote`` TUI
    read it at thread creation, so a session created or forked at ``ultra``
    otherwise starts (and reports in the TUI footer) that stale level.
    """
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    original = 'model_reasoning_effort = "medium"\n'
    (real_codex_home / "config.toml").write_text(original, encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    _disable_codex_startup_rpc(monkeypatch)

    server = _test_app_server(tmp_path, codex_home, tmp_path / "bridge", workspace)
    server.pinned_effort = "ultra"
    await server.start()
    await server.close()

    rendered = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert tomllib.loads(rendered)["model_reasoning_effort"] == "ultra"
    assert rendered.count("model_reasoning_effort") == 1
    assert (real_codex_home / "config.toml").read_text(encoding="utf-8") == original


class TestPinCodexConfigModel:
    """_pin_codex_config_model seeds the per-session config.toml model."""

    def test_replaces_top_level_model_only(self, tmp_path: Path) -> None:
        """The top-level ``model`` line is replaced; lookalike keys survive.

        ``model_provider`` / ``model_reasoning_effort`` also start with
        "model", and keys inside tables must never be touched — both were
        plausible regressions for a line-match implementation.
        """
        from omnigent.harnesses.codex_native.app_server import _pin_codex_config_model

        config = tmp_path / "config.toml"
        config.write_text(
            'model = "gpt-5.5"\n'
            'model_provider = "Databricks"\n'
            'model_reasoning_effort = "xhigh"\n'
            "[profiles.default]\n"
            'model = "table-scoped-stays"\n',
            encoding="utf-8",
        )
        _pin_codex_config_model(tmp_path, "databricks-gpt-5-4-mini")
        text = config.read_text(encoding="utf-8")
        assert 'model = "databricks-gpt-5-4-mini"' in text.splitlines()[0]
        assert 'model_provider = "Databricks"' in text
        assert 'model_reasoning_effort = "xhigh"' in text
        assert 'model = "table-scoped-stays"' in text
        assert "gpt-5.5" not in text

    def test_inserts_model_when_absent(self, tmp_path: Path) -> None:
        """A config with no top-level ``model`` gains one as the first line."""
        from omnigent.harnesses.codex_native.app_server import _pin_codex_config_model

        config = tmp_path / "config.toml"
        config.write_text("[profiles.default]\nx = 1\n", encoding="utf-8")
        _pin_codex_config_model(tmp_path, "gpt-5.5")
        lines = config.read_text(encoding="utf-8").splitlines()
        assert lines[0] == 'model = "gpt-5.5"'
        assert "[profiles.default]" in lines

    def test_materializes_symlink_without_touching_source(self, tmp_path: Path) -> None:
        """A symlinked config.toml is copied per-session; the shared source
        keeps its own model line (the live-caught clobber scenario)."""
        from omnigent.harnesses.codex_native.app_server import _pin_codex_config_model

        shared = tmp_path / "shared-config.toml"
        shared.write_text('model = "gpt-5.5"\n', encoding="utf-8")
        home = tmp_path / "codex-home"
        home.mkdir()
        (home / "config.toml").symlink_to(shared)
        _pin_codex_config_model(home, "databricks-gpt-5-4-mini")
        assert not (home / "config.toml").is_symlink()
        assert 'model = "databricks-gpt-5-4-mini"' in (home / "config.toml").read_text(
            encoding="utf-8"
        )
        assert shared.read_text(encoding="utf-8") == 'model = "gpt-5.5"\n'

    def test_read_back_by_forwarder_mirror_source(self, tmp_path: Path) -> None:
        """The forwarder's mirror source reads back exactly the pinned model.

        This is the regression the pin exists for: the mirror previously
        reported the shared file's stale model and overwrote the child's
        ``model_override``.
        """
        from omnigent.harnesses.codex_native.app_server import _pin_codex_config_model
        from omnigent.harnesses.codex_native.bridge import read_codex_config_model

        home = tmp_path / "codex-home"
        home.mkdir()
        (home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
        bridge_dir = tmp_path
        # read_codex_config_model resolves codex-home under the bridge dir.
        _pin_codex_config_model(home, "databricks-gpt-5-4-mini")
        assert read_codex_config_model(bridge_dir) == "databricks-gpt-5-4-mini"


class TestPinCodexConfigEffort:
    """_pin_codex_config_effort seeds the per-session config.toml effort."""

    def test_replaces_top_level_effort_only(self, tmp_path: Path) -> None:
        """The copied effort line is replaced; model and table-scoped keys survive."""
        from omnigent.harnesses.codex_native.app_server import _pin_codex_config_effort

        config = tmp_path / "config.toml"
        config.write_text(
            'model = "gpt-5.5"\n'
            'model_reasoning_effort = "medium"\n'
            "[profiles.default]\n"
            'model_reasoning_effort = "table-scoped-stays"\n',
            encoding="utf-8",
        )
        _pin_codex_config_effort(tmp_path, "ultra", "gpt-5.5")
        lines = config.read_text(encoding="utf-8").splitlines()
        assert lines[:2] == ['model = "gpt-5.5"', 'model_reasoning_effort = "ultra"']
        assert 'model_reasoning_effort = "table-scoped-stays"' in lines
        assert "medium" not in "\n".join(lines)

    @pytest.mark.parametrize(
        ("existing", "expected"),
        [
            (
                '  model_reasoning_effort = "medium"  # user default',
                '  model_reasoning_effort = "ultra"  # user default',
            ),
            ("model_reasoning_effort = 'medium'", 'model_reasoning_effort = "ultra"'),
        ],
        ids=["indented-with-comment", "single-quoted"],
    )
    def test_rewrites_the_existing_line_in_place(
        self, tmp_path: Path, existing: str, expected: str
    ) -> None:
        """An indented key keeps its indent and comment; an unparsable value is replaced whole.

        Missing the indented spelling would insert a second top-level key, which
        codex rejects as invalid TOML — the same regex the model pin uses to clamp
        this line already tolerates it.
        """
        from omnigent.harnesses.codex_native.app_server import _pin_codex_config_effort

        config = tmp_path / "config.toml"
        config.write_text(f"{existing}\n[profiles.default]\nx = 1\n", encoding="utf-8")
        _pin_codex_config_effort(tmp_path, "ultra", "gpt-5.6-sol")
        lines = config.read_text(encoding="utf-8").splitlines()
        assert lines[0] == expected
        assert sum("model_reasoning_effort" in line for line in lines) == 1
        assert (
            tomllib.loads(config.read_text(encoding="utf-8"))["model_reasoning_effort"] == "ultra"
        )

    def test_inserts_effort_when_absent(self, tmp_path: Path) -> None:
        """A config with no top-level effort gains one before the first table."""
        from omnigent.harnesses.codex_native.app_server import _pin_codex_config_effort

        config = tmp_path / "config.toml"
        config.write_text("[profiles.default]\nx = 1\n", encoding="utf-8")
        _pin_codex_config_effort(tmp_path, "high", None)
        lines = config.read_text(encoding="utf-8").splitlines()
        assert lines[0] == 'model_reasoning_effort = "high"'
        assert "[profiles.default]" in lines

    def test_clamps_to_the_pinned_models_ladder(self, tmp_path: Path) -> None:
        """A level the pinned model rejects is coerced so the first turn cannot 400."""
        from omnigent.harnesses.codex_native.app_server import _pin_codex_config_effort

        _pin_codex_config_effort(tmp_path, "ultra", "glm-5-2")
        assert (tmp_path / "config.toml").read_text(encoding="utf-8") == (
            'model_reasoning_effort = "medium"\n'
        )


@pytest.mark.parametrize(
    ("labels", "harness_override", "expected"),
    [
        ({"omnigent.routing.auto_harness": "1"}, None, True),
        # The sentinel survives until first-message routing resolves a harness.
        ({}, "auto", True),
        ({}, "codex-native", False),
        ({}, None, False),
    ],
    ids=["label", "sentinel", "pinned", "neither"],
)
async def test_codex_native_launch_config_reads_the_auto_harness_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    labels: dict[str, str],
    harness_override: str | None,
    expected: bool,
) -> None:
    """Only an auto-harness codex session gets the routed-spawn instructions."""
    import httpx

    from omnigent.runner.native.orchestration import _codex_native_launch_config
    from omnigent.runner.subagent_routing import AUTO_HARNESS_LABEL_KEY

    assert AUTO_HARNESS_LABEL_KEY == "omnigent.routing.auto_harness"
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:9999")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "workspace": str(tmp_path),
                "labels": labels,
                "harness_override": harness_override,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        config = await _codex_native_launch_config(session_id="conv_abc", server_client=client)

    assert config.auto_harness is expected


@pytest.mark.parametrize(
    ("persisted", "expected"),
    [("ultra", "ultra"), ("bogus", None), (None, None)],
    ids=["ultra", "unsupported", "unset"],
)
async def test_codex_native_launch_config_reads_reasoning_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persisted: str | None,
    expected: str | None,
) -> None:
    """The persisted effort reaches the launch; an unsupported one is dropped, not fatal."""
    import httpx

    from omnigent.runner.native.orchestration import _codex_native_launch_config

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:9999")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"workspace": str(tmp_path), "labels": {}, "reasoning_effort": persisted},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        config = await _codex_native_launch_config(session_id="conv_abc", server_client=client)

    assert config.reasoning_effort == expected


@pytest.mark.parametrize(
    ("model", "expected_effort"),
    [("gpt-5.6-sol", "ultra"), (None, "ultra"), ("glm-5-2", "medium")],
    ids=["sol", "no-model", "clamped"],
)
async def test_apply_codex_thread_effort_updates_the_loaded_thread(
    monkeypatch: pytest.MonkeyPatch, model: str | None, expected_effort: str
) -> None:
    """
    The persisted effort reaches a resumed thread via ``thread/settings/update``.

    A resumed thread runs the rollout's recorded effort — none after a runner
    restart rebuilt the rollout, the source's on a forked clone — so the launch
    re-applies the session's effort (clamped to the model's ladder) once the
    thread has started, instead of leaving the TUI at ``default`` until a web
    turn happens to send one.
    """
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    requests: list[tuple[str, dict[str, object]]] = []

    class _FakeClient:
        def __init__(self, *, ws_url: str, client_name: str) -> None:
            assert ws_url == "ws://127.0.0.1:9876"
            assert client_name == "omnigent-codex-native-effort"

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            requests.append((method, params))
            return {"result": {}}

    monkeypatch.setattr(codex_native_app_server, "CodexAppServerClient", _FakeClient)
    await codex_native_app_server.apply_codex_thread_effort(
        "ws://127.0.0.1:9876", "thread_abc", "ultra", model=model
    )
    assert requests == [
        ("thread/settings/update", {"threadId": "thread_abc", "effort": expected_effort})
    ]
