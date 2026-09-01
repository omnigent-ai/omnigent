"""Native Codex launches must stay on the provider-selected account home."""

from __future__ import annotations

from pathlib import Path

import pytest

import omnigent.codex_native as codex_native
import omnigent.codex_native_app_server as app_server
from omnigent.onboarding.provider_config import load_providers


def _entry(home: Path, *, kind: str = "subscription"):
    raw: dict[str, object] = {
        "kind": kind,
        "cli": "codex",
        "cli_home": str(home),
    }
    if kind == "cli-config":
        raw["model_provider"] = "WorkGateway"
    return load_providers({"providers": {"codex-work": raw}})["codex-work"]


def test_subscription_launch_uses_its_own_login_and_preserves_fail_fast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_home = tmp_path / "codex-work"
    work_home.mkdir()
    seen: list[Path] = []

    def _has_credential(path: Path) -> bool:
        seen.append(path)
        return False

    monkeypatch.setattr("omnigent.onboarding.ambient.codex_auth_has_credential", _has_credential)
    launch = app_server._resolve_subscription_launch(_entry(work_home), None, {})

    # Ambient fallback discovery may inspect the process-wide login afterward;
    # the selected subscription decision itself must use its own home first.
    assert seen[0] == work_home / "auth.json"
    assert launch.cli_home == work_home
    assert launch.login_required is True


def test_logged_in_subscription_keeps_its_home_without_login_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_home = tmp_path / "codex-work"
    work_home.mkdir()
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.codex_auth_has_credential",
        lambda path: path == work_home / "auth.json",
    )

    launch = app_server._resolve_subscription_launch(_entry(work_home), None, {})

    assert launch.cli_home == work_home
    assert launch.login_required is False


def test_cli_config_launch_carries_the_home_holding_its_provider_table(tmp_path: Path) -> None:
    work_home = tmp_path / "codex-work"

    launch = app_server._codex_provider_launch(_entry(work_home, kind="cli-config"), None)

    assert launch is not None
    assert launch.cli_home == work_home
    assert launch.config_overrides == ['model_provider="WorkGateway"']


def test_app_server_records_the_selected_config_source(tmp_path: Path) -> None:
    work_home = tmp_path / "codex-work"
    server = app_server.build_codex_native_server(
        socket_path=tmp_path / "app-server.sock",
        codex_home=tmp_path / "private",
        cwd=tmp_path,
        model=None,
        profile=None,
        bridge_dir=tmp_path / "bridge",
        codex_path="/usr/bin/codex",
        config_source_home=work_home,
    )

    assert server.config_source_home == work_home


def test_readiness_inspects_the_resolved_launch_home_without_a_secret_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_home = tmp_path / "codex-work"
    seen: list[Path] = []
    launch = app_server.NativeCodexLaunch(
        config_overrides=['model_provider="openai"'],
        model=None,
        profile=None,
        cli_home=work_home,
    )
    monkeypatch.setattr(codex_native, "_find_codex_cli", lambda: "/usr/bin/codex")
    monkeypatch.setattr(codex_native, "resolve_native_codex_launch", lambda **_kwargs: launch)
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_installed",
        lambda *_args, **_kwargs: True,
    )

    def _has_credential(path: Path) -> bool:
        seen.append(path)
        return True

    monkeypatch.setattr(codex_native, "_codex_auth_json_has_available_credential", _has_credential)

    assert codex_native._codex_auth_unavailable_reason() is None
    assert seen == [work_home / "auth.json"]


def test_model_catalog_fingerprint_is_separate_per_account_and_binary(
    tmp_path: Path,
) -> None:
    binary_a = tmp_path / "codex-a"
    binary_b = tmp_path / "codex-b"
    binary_a.write_bytes(b"a")
    binary_b.write_bytes(b"different")
    personal = app_server.NativeCodexLaunch([], None, None, cli_home=tmp_path / "personal")
    work = app_server.NativeCodexLaunch([], None, None, cli_home=tmp_path / "work")

    personal_key = app_server.codex_catalog_fingerprint(personal, codex_path=str(binary_a))
    work_key = app_server.codex_catalog_fingerprint(work, codex_path=str(binary_a))
    other_binary_key = app_server.codex_catalog_fingerprint(
        personal,
        codex_path=str(binary_b),
    )

    assert personal_key != work_key
    assert personal_key != other_binary_key


async def test_catalog_probe_keeps_the_preselected_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent import model_catalog_store

    launch = app_server.NativeCodexLaunch([], None, None, cli_home=tmp_path / "work")
    seen: list[app_server.NativeCodexLaunch | None] = []

    async def _probe(
        *,
        codex_path: str | None = None,
        launch: app_server.NativeCodexLaunch | None = None,
    ) -> list[dict[str, object]]:
        del codex_path
        seen.append(launch)
        return [{"id": "gpt-test"}]

    async def _ensure(
        _harness: str,
        _fingerprint: str,
        probe,
    ) -> list[dict[str, object]]:
        return await probe()

    monkeypatch.setattr(app_server, "probe_codex_model_options", _probe)
    monkeypatch.setattr(model_catalog_store, "ensure_catalog", _ensure)

    assert await app_server.codex_launch_catalog(launch=launch) == [{"id": "gpt-test"}]
    assert seen == [launch]


def test_probe_home_bridges_auth_and_config_from_the_selected_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_home = tmp_path / "home"
    source_home = tmp_path / "codex-work"
    source_home.mkdir()
    (source_home / "auth.json").write_text('{"OPENAI_API_KEY":"token"}')
    (source_home / "config.toml").write_text(
        'model_provider = "WorkGateway"\n[model_providers.WorkGateway]\nname = "Work"\n'
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: cache_home))

    probe_home = app_server._probe_codex_home(
        ['model_provider="WorkGateway"'],
        source_home,
    )

    assert (probe_home / "auth.json").resolve() == source_home / "auth.json"
    assert "WorkGateway" in (probe_home / "config.toml").read_text()
