"""Tests for the host-side provider / agent-pin operations.

These cover :mod:`omnigent.host.provider_ops` — the config-control-plane
core behind the ``host.provider_op`` frame and the
``/v1/hosts/{id}/providers*`` / ``.../agent-specs/{name}/pin`` routes.
All filesystem effects land in ``tmp_path``; no test touches a real
``~/.omnigent``.
"""

from __future__ import annotations

import json

import pytest
import yaml

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host import provider_ops


def _load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def _dump_yaml(path, data):
    with open(path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def _write_config(tmp_path, providers: dict) -> str:
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"providers": providers}, sort_keys=False))
    return str(config)


def _gateway_entry() -> dict:
    return {
        "kind": "gateway",
        "default": True,
        "openai": {
            "base_url": "https://gw.example/v1",
            "api_key_ref": "env:GW_KEY",
            "wire_api": "chat",
            "models": {"default": "gpt-x"},
        },
    }


class TestProvidersList:
    def test_missing_config_lists_empty(self, tmp_path) -> None:
        result = provider_ops.providers_list(config_path=str(tmp_path / "none.yaml"))
        assert result["providers"] == []

    def test_entries_are_redacted(self, tmp_path) -> None:
        path = _write_config(
            tmp_path,
            {
                "gw": {
                    "kind": "gateway",
                    "openai": {
                        "base_url": "https://gw.example/v1",
                        "api_key": "sk-super-secret",
                    },
                }
            },
        )
        result = provider_ops.providers_list(config_path=path)
        (entry,) = result["providers"]
        assert entry["name"] == "gw"
        assert "api_key" not in entry["openai"]
        assert entry["openai"]["api_key_set"] is True
        assert "sk-super-secret" not in json.dumps(entry)


class TestProviderUpsert:
    def test_upsert_writes_and_round_trips(self, tmp_path) -> None:
        path = str(tmp_path / "config.yaml")
        result = provider_ops.provider_upsert("gw", _gateway_entry(), config_path=path)
        assert result["name"] == "gw"
        raw = _load_yaml(path)
        assert raw["providers"]["gw"]["openai"]["base_url"] == "https://gw.example/v1"
        # key order of the entry survives the round trip
        assert list(raw["providers"]["gw"]) == list(_gateway_entry())

    def test_upsert_rejects_shape_the_runtime_rejects(self, tmp_path) -> None:
        path = _write_config(tmp_path, {})
        with pytest.raises(OmnigentError) as err:
            provider_ops.provider_upsert(
                "bad",
                {"kind": "not-a-kind"},
                config_path=path,
            )
        assert err.value.code == ErrorCode.INVALID_INPUT
        assert _load_yaml(path)["providers"] == {}

    def test_upsert_rejects_bad_name(self, tmp_path) -> None:
        path = _write_config(tmp_path, {})
        for bad in ("", "../evil", "a/b", None):
            with pytest.raises(OmnigentError):
                provider_ops.provider_upsert(bad, _gateway_entry(), config_path=path)

    def test_upsert_backs_up_previous_config(self, tmp_path) -> None:
        path = _write_config(tmp_path, {"old": {"kind": "key"}})
        provider_ops.provider_upsert("gw", _gateway_entry(), config_path=path)
        backups = list(tmp_path.glob("config.yaml.bak-*"))
        assert len(backups) == 1
        assert "old" in yaml.safe_load(backups[0].read_text())["providers"]


class TestProviderDelete:
    def test_delete_removes_entry(self, tmp_path) -> None:
        path = _write_config(tmp_path, {"gw": _gateway_entry()})
        result = provider_ops.provider_delete("gw", config_path=path)
        assert result["name"] == "gw"
        assert _load_yaml(path)["providers"] == {}

    def test_delete_unknown_is_not_found(self, tmp_path) -> None:
        path = _write_config(tmp_path, {})
        with pytest.raises(OmnigentError) as err:
            provider_ops.provider_delete("nope", config_path=path)
        assert err.value.code == ErrorCode.NOT_FOUND


class TestProviderTest:
    def test_unknown_provider_is_not_found(self, tmp_path) -> None:
        path = _write_config(tmp_path, {})
        with pytest.raises(OmnigentError) as err:
            provider_ops.provider_test("nope", config_path=path)
        assert err.value.code == ErrorCode.NOT_FOUND

    def test_subscription_kind_has_no_endpoint(self, tmp_path) -> None:
        path = _write_config(tmp_path, {"sub": {"kind": "subscription", "cli": "claude"}})
        with pytest.raises(OmnigentError) as err:
            provider_ops.provider_test("sub", config_path=path)
        assert err.value.code == ErrorCode.INVALID_INPUT

    def test_probe_hits_models_endpoint_with_bearer(self, tmp_path, monkeypatch) -> None:
        path = _write_config(tmp_path, {"gw": _gateway_entry()})
        seen: dict = {}

        class _Response:
            status_code = 200

            def json(self):
                return {
                    "data": [{"id": "gpt-x"}, {"id": "gpt-y"}, {"nope": True}]
                }

        def fake_get(url, headers=None, timeout=None):
            seen["url"] = url
            seen["headers"] = headers
            return _Response()

        monkeypatch.setattr(provider_ops.httpx, "get", fake_get)
        monkeypatch.setenv("GW_KEY", "sk-test-value")
        result = provider_ops.provider_test("gw", config_path=path)
        assert seen["url"] == "https://gw.example/v1/models"
        assert seen["headers"]["Authorization"] == "Bearer sk-test-value"
        assert result["ok"] is True
        assert result["models"] == ["gpt-x", "gpt-y"]
        assert "sk-test-value" not in json.dumps(result)

    def test_probe_reports_connection_failure(self, tmp_path, monkeypatch) -> None:
        path = _write_config(tmp_path, {"gw": _gateway_entry()})
        monkeypatch.setenv("GW_KEY", "sk-test-value")

        def fake_get(url, headers=None, timeout=None):
            raise provider_ops.httpx.ConnectError("refused")

        monkeypatch.setattr(provider_ops.httpx, "get", fake_get)
        result = provider_ops.provider_test("gw", config_path=path)
        assert result["ok"] is False
        assert "refused" in result["error"]


def _bundle_agent(tmp_path, name: str = "my-agent") -> str:
    agents = tmp_path / "agents" / name
    agents.mkdir(parents=True)
    (agents / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "spec_version": 1,
                "name": name,
                "prompt": "hi",
                "executor": {
                    "type": "omnigent",
                    "config": {"harness": "pi"},
                    "skills": [],
                },
            },
            sort_keys=False,
        )
    )
    return str(tmp_path / "agents")


def _single_file_agent(tmp_path, name: str = "solo") -> str:
    agents = tmp_path / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {"name": name, "prompt": "hi", "executor": {"harness": "claude-sdk"}},
            sort_keys=False,
        )
    )
    return str(agents)


class TestAgentsList:
    def test_lists_bundles_and_single_files(self, tmp_path) -> None:
        agents_dir = _bundle_agent(tmp_path)
        _single_file_agent(tmp_path)
        result = provider_ops.agents_list(agents_dir=agents_dir)
        names = {a["name"] for a in result["agents"]}
        assert names == {"my-agent", "solo"}
        (bundle,) = [a for a in result["agents"] if a["name"] == "my-agent"]
        assert bundle["harness"] == "pi"
        assert bundle["spec_version"] == 1

    def test_missing_dir_lists_empty(self, tmp_path) -> None:
        result = provider_ops.agents_list(agents_dir=str(tmp_path / "absent"))
        assert result["agents"] == []


class TestAgentPin:
    def test_pin_sets_provider_auth_and_model(self, tmp_path) -> None:
        agents_dir = _bundle_agent(tmp_path)
        result = provider_ops.agent_pin_set(
            "my-agent", provider="openrouter", model="gpt-x", agents_dir=agents_dir
        )
        assert result["spec"]["auth"] == {"type": "provider", "name": "openrouter"}
        assert result["spec"]["model"] == "gpt-x"
        raw = _load_yaml(f"{agents_dir}/my-agent/config.yaml")
        # sibling keys untouched
        assert raw["executor"]["config"]["harness"] == "pi"
        assert raw["executor"]["skills"] == []

    def test_pin_creates_backup(self, tmp_path) -> None:
        agents_dir = _bundle_agent(tmp_path)
        result = provider_ops.agent_pin_set("my-agent", model="gpt-x", agents_dir=agents_dir)
        assert result["backup"]
        backups = list((tmp_path / "agents" / "my-agent").glob("config.yaml.bak-*"))
        assert len(backups) == 1

    def test_pin_single_file_agent(self, tmp_path) -> None:
        agents_dir = _single_file_agent(tmp_path)
        result = provider_ops.agent_pin_set("solo", model="claude-x", agents_dir=agents_dir)
        assert result["spec"]["model"] == "claude-x"

    def test_pin_unknown_agent_is_not_found(self, tmp_path) -> None:
        agents_dir = _bundle_agent(tmp_path)
        with pytest.raises(OmnigentError) as err:
            provider_ops.agent_pin_set("ghost", model="m", agents_dir=agents_dir)
        assert err.value.code == ErrorCode.NOT_FOUND

    def test_pin_requires_something_to_set(self, tmp_path) -> None:
        agents_dir = _bundle_agent(tmp_path)
        with pytest.raises(OmnigentError) as err:
            provider_ops.agent_pin_set("my-agent", agents_dir=agents_dir)
        assert err.value.code == ErrorCode.INVALID_INPUT

    def test_clear_removes_provider_pin_but_keeps_inline_auth(self, tmp_path) -> None:
        agents_dir = _bundle_agent(tmp_path)
        provider_ops.agent_pin_set("my-agent", provider="openrouter", agents_dir=agents_dir)
        # a spec with an inline api_key auth block must keep it
        path = f"{agents_dir}/my-agent/config.yaml"
        raw = _load_yaml(path)
        raw["executor"]["auth"] = {"type": "api_key", "api_key": "sk-inline"}
        _dump_yaml(path, raw)

        provider_ops.agent_pin_set("my-agent", provider="9router", agents_dir=agents_dir)
        result = provider_ops.agent_pin_clear("my-agent", agents_dir=agents_dir)
        assert result["spec"]["auth"] is None or (
            isinstance(result["spec"]["auth"], dict)
            and result["spec"]["auth"].get("type") != "provider"
        )
        # model pin was cleared too
        assert result["spec"]["model"] is None

    def test_clear_keeps_model_when_only_provider_requested(self, tmp_path) -> None:
        agents_dir = _bundle_agent(tmp_path)
        provider_ops.agent_pin_set(
            "my-agent", provider="openrouter", model="gpt-x", agents_dir=agents_dir
        )
        result = provider_ops.agent_pin_clear(
            "my-agent", provider=True, model=False, agents_dir=agents_dir
        )
        assert result["spec"]["model"] == "gpt-x"
        assert result["spec"]["auth"] is None


class TestRunProviderOp:
    def test_unknown_op_is_invalid_input(self) -> None:
        with pytest.raises(OmnigentError) as err:
            provider_ops.run_provider_op("reformat_disk", {})
        assert err.value.code == ErrorCode.INVALID_INPUT

    def test_wire_params_cannot_redirect_config_path(
        self, tmp_path, monkeypatch
    ) -> None:
        """``config_path`` in frame params is ignored — ops hit the real file.

        A remote caller must not be able to point a write at an arbitrary
        path on the host; the dispatch always uses the host's own
        ``OMNIGENT_CONFIG_HOME`` (here: a temp home the test controls).
        """
        real_home = tmp_path / "home"
        (real_home / ".omnigent").mkdir(parents=True)
        monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(real_home / ".omnigent"))

        evil = tmp_path / "evil.yaml"
        evil.write_text("{}")
        provider_ops.run_provider_op(
            "provider_upsert",
            {"name": "gw", "entry": _gateway_entry(), "config_path": str(evil)},
        )
        # the write landed in the host's config, not the attacker-named file
        written = _load_yaml(real_home / ".omnigent" / "config.yaml")
        assert "gw" in written["providers"]
        assert yaml.safe_load(evil.read_text()) == {}

    def test_wire_params_cannot_redirect_agents_dir(self, tmp_path, monkeypatch) -> None:
        real_home = tmp_path / "home"
        (real_home / ".omnigent").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(real_home))
        evil = tmp_path / "evil-agents"
        bundle = evil / "my-agent"
        bundle.mkdir(parents=True)
        (bundle / "config.yaml").write_text(
            yaml.safe_dump({"spec_version": 1, "name": "my-agent", "executor": {}})
        )
        # The op must look at the host's real (empty) agents dir — hence
        # NOT_FOUND — and never consult the attacker-named directory.
        with pytest.raises(OmnigentError) as err:
            provider_ops.run_provider_op(
                "agent_pin_set",
                {
                    "agent": "my-agent",
                    "model": "gpt-x",
                    "agents_dir": str(evil),
                },
            )
        assert err.value.code == ErrorCode.NOT_FOUND
        assert list(evil.rglob("*.bak-*")) == []
        assert _load_yaml(evil / "my-agent" / "config.yaml")["executor"].get(
            "model"
        ) is None
