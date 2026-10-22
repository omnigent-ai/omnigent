"""Snapshot validation and local import using real config files."""

import json

import pytest
import yaml
from click.testing import CliRunner

from omnigent.configuration_commands import cloud
from omnigent.configuration_profile import (
    ConfigurationSnapshot,
    PluginOptOuts,
    PortableConfiguration,
)
from omnigent.preference_sync import PreferenceNamespace


def test_portable_roundtrip_preserves_local_only_values():
    original = {
        "model": "model-a",
        "opencode_model": "provider/model",
        "harness": {
            "default": "pi",
            "codex-native": {
                "args": ["--config", "x", "--config", "y"],
                "command": "/local/codex",
            },
        },
        "providers": {"secret": "local-only"},
        "server": "http://local",
        "host": {"id": "a"},
    }
    profile = PortableConfiguration.from_config(original)
    assert "local-only" not in profile.model_dump_json()
    assert "/local/codex" not in profile.model_dump_json()
    target = {
        "model": "old",
        "harness": {"codex-native": {"command": "/other/codex", "args": []}},
        "host": {"id": "b"},
    }
    applied = profile.apply_to(target)
    assert applied["host"] == {"id": "b"}
    assert applied["harness"]["codex-native"] == {
        "command": "/other/codex",
        "args": ["--config", "x", "--config", "y"],
    }
    assert target["model"] == "old"
    assert profile.apply_to(applied) == applied
    cleared = PortableConfiguration().apply_to(applied)
    assert "model" not in cleared and "opencode_model" not in cleared
    assert cleared["harness"] == {"codex-native": {"command": "/other/codex"}}


@pytest.mark.parametrize(
    "payload",
    [
        {"providers": {"token": "secret"}},
        {"harness_args": {"default": []}},
        {"harness_args": {"claude": [], "claude-sdk": ["x"]}},
        {"harness_args": {"codex-native": [False]}},
        {"harness_args": {"codex-native": ["x" * 12000]}},
        {"plugin_opt_outs": {"opted_out": ["x@market"], "disable_all": False}},
        {"plugin_opt_outs": {"opted_out": [], "disable_all": "false"}},
    ],
)
def test_invalid_configuration_is_rejected(payload):
    with pytest.raises(ValueError):
        PortableConfiguration.model_validate(payload)


@pytest.mark.parametrize(
    "config",
    [
        {"model": False},
        {"opencode_model": []},
        {"harness": 1},
        {"harness": {"default": False}},
        {"harness": {"pi": "bad"}},
        {"harness": {"pi": {"args": [False]}}},
    ],
)
def test_invalid_local_values_cannot_be_saved_as_missing_defaults(config):
    with pytest.raises(ValueError):
        PortableConfiguration.from_config(config)


@pytest.mark.parametrize(
    "entries",
    [
        {"claude": {"args": ["--verbose"]}},
        {"claude": {"args": ["--verbose"]}, "claude-sdk": {"args": ["--debug"]}},
        {"claude-sdk": {"args": ["--debug"]}, "claude": {"args": []}},
        {"claude": {"args": ["--verbose"]}, "claude-sdk": {"command": "/source/claude"}},
    ],
)
def test_imported_arguments_preserve_alias_commands_at_launch_resolution(entries):
    from omnigent.harness_startup_config import resolve_harness_config

    source = {"harness": entries}
    _, source_overrides = resolve_harness_config(source)
    configuration = PortableConfiguration.from_config(source)
    applied = configuration.apply_to(
        {"harness": {"claude": {"command": "/local/claude", "args": ["old"]}}}
    )
    _, overrides = resolve_harness_config(applied)
    assert overrides["claude-sdk"] == {
        "command": "/local/claude",
        "args": source_overrides["claude-sdk"]["args"],
    }


@pytest.mark.parametrize(
    "scenario",
    [
        "accept",
        "decline",
        "cloud_changed",
        "login_changed",
        "local_changed",
        "write_failure",
        "malformed_plugins",
    ],
)
def test_reviewed_import_and_recovery(tmp_path, monkeypatch, scenario):
    import omnigent.configuration_commands as commands

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "config.yaml"
    plugins = tmp_path / "plugin-optout.json"
    original = (
        b"model: old\nproviders:\n  private: keep-me\n"
        b"harness:\n  codex-native:\n    command: /local/codex\n"
    )
    config.write_bytes(original)
    plugin_original = b'{"opted_out": ["old"], "disable_all": true}'
    plugins.write_bytes(plugin_original)
    snapshot = ConfigurationSnapshot(
        identity=PreferenceNamespace(authority_id="a", user_id="u", workspace_id="1"),
        revision=1,
        configuration=PortableConfiguration(
            model="new",
            harness_args={"codex-native": []},
            plugin_opt_outs=PluginOptOuts(opted_out=["lite"], disable_all=False),
        ),
    )
    calls = []

    async def request(server, operation=None):
        assert operation is None
        calls.append(server)
        if len(calls) == 2:
            if scenario == "cloud_changed":
                return snapshot.model_copy(update={"revision": 2})
            if scenario == "login_changed":
                return snapshot.model_copy(
                    update={"identity": snapshot.identity.model_copy(update={"user_id": "other"})}
                )
            if scenario == "local_changed":
                config.write_bytes(b"model: edited\n")
        return snapshot

    monkeypatch.setattr(commands, "_request", request)
    if scenario == "write_failure":
        write = commands._write_private

        def fail_plugin(path, data):
            if path == plugins:
                raise OSError("disk failure")
            write(path, data)

        monkeypatch.setattr(commands, "_write_private", fail_plugin)
    if scenario == "malformed_plugins":
        plugins.write_text("broken")
    result = CliRunner().invoke(
        cloud,
        ["--server", "http://local", "--plugin-opt-outs", str(plugins), "import"],
        input="n\n" if scenario == "decline" else "y\n",
    )
    if scenario == "accept":
        assert result.exit_code == 0, result.output
        data = yaml.safe_load(config.read_text())
        assert data["model"] == "new" and data["providers"] == {"private": "keep-me"}
        assert data["harness"]["codex-native"] == {"command": "/local/codex", "args": []}
        assert json.loads(plugins.read_text()) == {"opted_out": ["lite"], "disable_all": False}
        assert next(tmp_path.glob("config.yaml.before-import-*")).read_bytes() == original
        assert (
            next(tmp_path.glob("plugin-optout.json.before-import-*")).read_bytes()
            == plugin_original
        )
        assert config.stat().st_mode & 0o777 == 0o600
    else:
        assert result.exit_code != 0
        assert config.read_bytes() == (
            b"model: edited\n" if scenario == "local_changed" else original
        )
        if scenario != "malformed_plugins":
            assert plugins.read_bytes() == plugin_original


@pytest.mark.parametrize("oversized", [False, True])
def test_saving_without_plugin_file_keeps_existing_plugin_preferences(
    tmp_path, monkeypatch, oversized
):
    import omnigent.configuration_commands as commands

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    local = {"model": "new", "providers": {"private": "local-only"}}
    if oversized:
        local["harness"] = {"codex-native": {"args": ["x" * 11000]}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(local))
    plugins = PluginOptOuts(
        opted_out=[f"plugin-{number:03d}-" + "p" * 100 for number in range(20)]
        if oversized
        else ["lite"],
        disable_all=True,
    )
    snapshot = ConfigurationSnapshot(
        identity=PreferenceNamespace(authority_id="a", user_id="u", workspace_id="1"),
        revision=1,
        configuration=PortableConfiguration(model="old", plugin_opt_outs=plugins),
    )

    async def request(server, operation=None):
        if operation is None:
            return snapshot
        assert not oversized, "Oversized snapshots must be rejected before upload"
        assert operation.configuration.plugin_opt_outs == plugins
        assert operation.configuration.model == "new"
        assert "local-only" not in operation.model_dump_json()
        return snapshot.model_copy(
            update={"configuration": operation.configuration, "revision": 2}
        )

    monkeypatch.setattr(commands, "_request", request)
    result = CliRunner().invoke(cloud, ["--server", "http://local", "save", "--yes"])
    if oversized:
        assert result.exit_code == 1 and "exceeds 12000 bytes" in result.output
    else:
        assert result.exit_code == 0, result.output


@pytest.mark.parametrize("userinfo", ["user", "user:password", ":password", ""])
def test_cloud_rejects_url_userinfo_before_contacting_server(tmp_path, monkeypatch, userinfo):
    import omnigent.configuration_commands as commands

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(
        commands, "_request", lambda *_args: pytest.fail("invalid URL must not be contacted")
    )
    result = CliRunner().invoke(cloud, ["--server", f"https://{userinfo}@example.test", "show"])
    assert result.exit_code == 1
    assert "without credentials" in result.output


@pytest.mark.parametrize("interactive", [True, False])
def test_onboarding_is_one_time_and_noninteractive_startup_is_idle(
    tmp_path, monkeypatch, interactive
):
    import omnigent.configuration_commands as commands

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    snapshot = ConfigurationSnapshot(
        identity=PreferenceNamespace(authority_id="a", user_id="u", workspace_id="1"),
        configuration=PortableConfiguration(model="saved"),
        revision=1,
    )
    calls = []

    async def request(server, operation=None):
        calls.append(server)
        return snapshot

    monkeypatch.setattr(commands, "_request", request)
    runner = CliRunner()
    with runner.isolation(input="n\n"):
        monkeypatch.setattr(commands.sys.stdin, "isatty", lambda: interactive)
        monkeypatch.setattr(commands.sys.stdout, "isatty", lambda: interactive)
        cloud.main(["--server", "http://local", "onboard"], standalone_mode=False)
        cloud.main(["--server", "http://local", "onboard"], standalone_mode=False)
    assert len(calls) == (1 if interactive else 0)
    assert not (tmp_path / "config.yaml").exists()


def test_launcher_can_check_onboarding_without_network_and_env_selects_server(
    tmp_path, monkeypatch
):
    import omnigent.configuration_commands as commands

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_SERVER", "http://configured")
    monkeypatch.setattr(
        commands, "_request", lambda *_args: pytest.fail("unexpected network request")
    )
    runner = CliRunner()
    result = runner.invoke(cloud, ["onboard", "--check"])
    assert result.exit_code == 0 and result.output.strip() == "pending"
    marker = commands.onboarding_marker("http://configured")
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    result = runner.invoke(cloud, ["onboard", "--check"])
    assert result.exit_code == 0 and result.output.strip() == "complete"
    monkeypatch.setenv("OMNIGENT_API_KEY", "another-login")
    result = runner.invoke(cloud, ["onboard", "--check"])
    assert result.exit_code == 0 and result.output.strip() == "pending"


async def test_workspace_selector_uses_normal_auth_and_routing(tmp_path, monkeypatch):
    import httpx

    import omnigent.configuration_commands as commands
    import omnigent.runner._entry as entry
    from omnigent.cli_auth import (
        load_databricks_org_id,
        open_server_client,
        store_databricks_org_id,
    )

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    base = "https://workspace.example/api/2.0/omnigent"
    store_databricks_org_id(base, "111")
    identity = PreferenceNamespace(authority_id="a", user_id="u", workspace_id="222")

    def token_factory(**options):
        assert options["server_url"] == base
        assert load_databricks_org_id(base) == "222"
        return lambda: "test-token"

    def respond(request):
        assert str(request.url) == base + "/v1/preferences/configuration"
        assert request.headers["X-Databricks-Org-Id"] == "222"
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200, json=ConfigurationSnapshot(identity=identity).model_dump())

    monkeypatch.setattr(entry, "_make_auth_token_factory", token_factory)
    monkeypatch.setattr(
        commands,
        "open_server_client",
        lambda *args, **kwargs: open_server_client(
            *args, **kwargs, transport=httpx.MockTransport(respond)
        ),
    )
    result = await commands._request(base + "?o=222")
    assert result.identity == identity
    assert commands.onboarding_marker(base + "?o=222") == commands.onboarding_marker(base)
