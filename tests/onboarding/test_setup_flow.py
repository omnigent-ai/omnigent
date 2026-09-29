"""Tests for the profile-config and interactive flows in :mod:`omnigent.onboarding.setup`.

``test_setup.py`` covers env hygiene, CLI discovery and workspace login. This
module covers the ``~/.databrickscfg`` helpers, profile classification, the
OAuth login wrapper, and the ``run_onboarding`` / ``maybe_run_onboarding``
prompts. The internal-beta profile catalog is stubbed (it is absent from the
OSS build), and no real ``databricks`` CLI or browser flow is involved.
"""

from __future__ import annotations

import configparser
import io
import subprocess
import sys
import types
from pathlib import Path

import pytest
from rich.console import Console

from omnigent.onboarding import setup as setup_mod
from omnigent.onboarding.setup import (
    SKIP_ENV_VAR,
    ProfileSpec,
    _alias_profile,
    _alias_source_for,
    _apply_silent_aliases,
    _compute_actions,
    _databrickscfg_path,
    _derive_workspace_profile_name,
    _handle_wrong_host,
    _host_matches,
    _login_profile,
    _remove_profile_section,
    maybe_run_onboarding,
    run_onboarding,
)

_OSS = ProfileSpec("oss", "https://oss.example.com", "OSS gateway", True)
_JIRA = ProfileSpec("jira", "https://jira.example.com", "Jira MCP", False)
_CLI = "/usr/bin/databricks"


@pytest.fixture()
def catalog(monkeypatch: pytest.MonkeyPatch) -> tuple[ProfileSpec, ...]:
    """Stub the internal-beta catalog with two known profiles."""
    stub = types.ModuleType("omnigent.onboarding.internal_beta")
    stub.DEFAULT_PROFILES = (_OSS, _JIRA)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omnigent.onboarding.internal_beta", stub)
    return stub.DEFAULT_PROFILES  # type: ignore[attr-defined]


@pytest.fixture()
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "databrickscfg"
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(path))
    return path


def _write_cfg(path: Path, sections: dict[str, dict[str, str]]) -> None:
    cfg = configparser.ConfigParser()
    cfg.read_dict(sections)
    with path.open("w") as f:
        cfg.write(f)


def _read_cfg(path: Path) -> dict[str, dict[str, str]]:
    cfg = configparser.ConfigParser()
    cfg.read(path)
    return {name: dict(cfg[name]) for name in cfg.sections()}


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=200, no_color=True), buf


# ── host helpers ─────────────────────────────────────────────


def test_host_matches_ignores_trailing_slashes() -> None:
    assert _host_matches("https://a.example.com/", "https://a.example.com")
    assert not _host_matches("https://a.example.com", "https://b.example.com")


def test_alias_source_skips_target_itself_and_other_hosts() -> None:
    existing = {
        "oss": "https://oss.example.com",
        "mine": "https://oss.example.com/",
        "x": "https://x",
    }

    assert _alias_source_for("https://oss.example.com", "oss", existing) == "mine"
    assert _alias_source_for("https://oss.example.com", "other", existing) == "oss"
    assert _alias_source_for("https://nowhere.example.com", "oss", existing) is None


# ── ~/.databrickscfg helpers ─────────────────────────────────


def test_databrickscfg_path_honors_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", "~/custom.cfg")
    # ``expanduser`` resolves ``~`` from the environment, not ``Path.home``.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert _databrickscfg_path() == tmp_path / "custom.cfg"


def test_databrickscfg_path_defaults_to_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DATABRICKS_CONFIG_FILE", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert _databrickscfg_path() == tmp_path / ".databrickscfg"


def test_alias_profile_copies_section_and_keeps_others(cfg_path: Path) -> None:
    _write_cfg(
        cfg_path,
        {
            "src": {"host": "https://oss.example.com", "auth_type": "databricks-cli"},
            "keep": {"a": "1"},
        },
    )

    _alias_profile("src", "oss")

    cfg = _read_cfg(cfg_path)
    assert (
        cfg["oss"]
        == cfg["src"]
        == {"host": "https://oss.example.com", "auth_type": "databricks-cli"}
    )
    assert cfg["keep"] == {"a": "1"}
    # The atomic-write temp file is gone.
    assert not cfg_path.with_name(cfg_path.name + ".write").exists()


def test_alias_profile_rejects_unknown_source(cfg_path: Path) -> None:
    _write_cfg(cfg_path, {"src": {"host": "h"}})

    with pytest.raises(ValueError, match="alias source 'nope'"):
        _alias_profile("nope", "oss")
    assert "oss" not in _read_cfg(cfg_path)


def test_remove_profile_section_reports_whether_it_removed(cfg_path: Path) -> None:
    assert _remove_profile_section("oss") is False  # no file at all

    _write_cfg(cfg_path, {"oss": {"host": "h"}, "keep": {"a": "1"}})
    assert _remove_profile_section("missing") is False
    assert _remove_profile_section("oss") is True

    assert _read_cfg(cfg_path) == {"keep": {"a": "1"}}


# ── classification ───────────────────────────────────────────


def test_compute_actions_buckets_each_profile(catalog: tuple[ProfileSpec, ...]) -> None:
    ready = _compute_actions(
        {"oss": "https://oss.example.com/", "jira": "https://jira.example.com"}
    )
    aliasable = _compute_actions(
        {"mine": "https://oss.example.com", "jira": "https://jira.example.com"}
    )
    wrong = _compute_actions({"oss": "https://elsewhere.example.com"})
    fresh = _compute_actions({})

    assert ready.ready == (_OSS, _JIRA) and not ready.oauth and not ready.aliasable
    assert aliasable.aliasable == (("mine", _OSS),) and aliasable.ready == (_JIRA,)
    assert wrong.wrong_host == ((_OSS, "https://elsewhere.example.com"),)
    assert wrong.oauth == (_JIRA,)
    assert fresh.oauth == (_OSS, _JIRA)


def test_derive_profile_name_prefers_existing_then_bundled_then_dns_label(
    catalog: tuple[ProfileSpec, ...],
) -> None:
    # An existing login for the host wins, even over the bundled canonical name.
    assert (
        _derive_workspace_profile_name(
            "https://oss.example.com", {"mine": "https://oss.example.com/"}
        )
        == "mine"
    )
    assert _derive_workspace_profile_name("https://oss.example.com", {}) == "oss"
    assert _derive_workspace_profile_name("https://my-ws.cloud.databricks.com", {}) == "my-ws"


def test_derive_profile_name_falls_back_when_url_has_no_host(
    catalog: tuple[ProfileSpec, ...],
) -> None:
    assert _derive_workspace_profile_name("not-a-url", {}) == "databricks"


def test_derive_profile_name_without_internal_beta_module(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``None`` in sys.modules makes the import raise, like the OSS build.
    monkeypatch.setitem(sys.modules, "omnigent.onboarding.internal_beta", None)

    assert _derive_workspace_profile_name("https://oss.example.com", {}) == "oss"


# ── OAuth login wrapper ──────────────────────────────────────


def _run_returning(returncode: int, calls: list[list[str]]):
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode)

    return fake_run


def test_login_profile_success_logs_in_then_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    sleeps: list[float] = []
    monkeypatch.setattr(subprocess, "run", _run_returning(0, calls))
    monkeypatch.setattr(setup_mod.time, "sleep", sleeps.append)
    console, out = _console()

    assert _login_profile(_CLI, _OSS, console) is True

    assert calls == [[_CLI, "auth", "login", "--host", _OSS.host, "--profile", "oss"]]
    assert sleeps == [setup_mod._OAUTH_BETWEEN_LOGIN_SLEEP_SECONDS]
    assert "✓ oss" in out.getvalue()


def test_login_profile_reports_failure_without_pausing(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(subprocess, "run", _run_returning(2, []))
    monkeypatch.setattr(setup_mod.time, "sleep", sleeps.append)
    console, out = _console()

    assert _login_profile(_CLI, _OSS, console) is False

    assert "failed (exit 2)" in out.getvalue()
    assert sleeps == []


def test_login_profile_handles_ctrl_c(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(argv: list[str], **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "run", interrupted)
    console, out = _console()

    assert _login_profile(_CLI, _OSS, console) is False
    assert "cancelled oss" in out.getvalue()


# ── silent aliasing ──────────────────────────────────────────


def test_apply_silent_aliases_counts_successes_and_reports_failures(cfg_path: Path) -> None:
    _write_cfg(cfg_path, {"mine": {"host": "https://oss.example.com"}})
    console, out = _console()

    landed = _apply_silent_aliases((("mine", _OSS), ("ghost", _JIRA)), console)

    assert landed == 1
    assert "oss" in _read_cfg(cfg_path)
    assert "could not alias jira from ghost" in out.getvalue()


# ── wrong-host prompt ────────────────────────────────────────


@pytest.fixture()
def login_calls(monkeypatch: pytest.MonkeyPatch) -> list[ProfileSpec]:
    calls: list[ProfileSpec] = []
    monkeypatch.setattr(
        setup_mod, "_login_profile", lambda cli, spec, console: calls.append(spec) or True
    )
    monkeypatch.setattr(setup_mod, "_remove_profile_section", lambda name: True)
    return calls


def test_wrong_host_skips_on_non_tty(
    monkeypatch: pytest.MonkeyPatch, login_calls: list[ProfileSpec]
) -> None:
    monkeypatch.setattr(setup_mod.sys.stdin, "isatty", lambda: False)
    console, out = _console()

    _handle_wrong_host(_CLI, _OSS, "https://old.example.com", console)

    assert login_calls == []
    assert "non-TTY" in out.getvalue()
    assert "current:  https://old.example.com" in out.getvalue()


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_wrong_host_leaves_profile_on_interrupted_prompt(
    monkeypatch: pytest.MonkeyPatch, login_calls: list[ProfileSpec], exc: type[BaseException]
) -> None:
    monkeypatch.setattr(setup_mod.sys.stdin, "isatty", lambda: True)

    def interrupted(prompt: str) -> str:
        raise exc

    monkeypatch.setattr("builtins.input", interrupted)

    _handle_wrong_host(_CLI, _OSS, "https://old.example.com", _console()[0])

    assert login_calls == []


@pytest.mark.parametrize("answer", ["", "n", "no", "maybe"])
def test_wrong_host_declined_keeps_existing_profile(
    monkeypatch: pytest.MonkeyPatch, login_calls: list[ProfileSpec], answer: str
) -> None:
    monkeypatch.setattr(setup_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    console, out = _console()

    _handle_wrong_host(_CLI, _OSS, "https://old.example.com", console)

    assert login_calls == []
    assert "Skipped `oss`" in out.getvalue()


@pytest.mark.parametrize("answer", ["y", " YES "])
def test_wrong_host_overwrite_drops_section_then_logs_in(
    monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    order: list[str] = []
    monkeypatch.setattr(setup_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    monkeypatch.setattr(
        setup_mod, "_remove_profile_section", lambda name: order.append(f"remove:{name}") or True
    )
    monkeypatch.setattr(
        setup_mod,
        "_login_profile",
        lambda cli, spec, console: order.append(f"login:{spec.name}") or True,
    )

    _handle_wrong_host(_CLI, _OSS, "https://old.example.com", _console()[0])

    assert order == ["remove:oss", "login:oss"]


# ── run_onboarding ───────────────────────────────────────────


def _hosts_sequence(monkeypatch: pytest.MonkeyPatch, *snapshots: dict[str, str]) -> list[int]:
    """Serve successive ``_existing_profile_hosts`` snapshots (last one repeats)."""
    remaining = list(snapshots)
    reads = [0]

    def fake() -> dict[str, str]:
        reads[0] += 1
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    monkeypatch.setattr(setup_mod, "_existing_profile_hosts", fake)
    return reads


READY = {"oss": "https://oss.example.com", "jira": "https://jira.example.com"}


def test_run_onboarding_honors_skip_env(
    monkeypatch: pytest.MonkeyPatch, catalog: tuple[ProfileSpec, ...]
) -> None:
    monkeypatch.setenv(SKIP_ENV_VAR, "1")
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: pytest.fail("CLI consulted"))

    assert run_onboarding() is False


def test_run_onboarding_explains_missing_cli(
    monkeypatch: pytest.MonkeyPatch,
    catalog: tuple[ProfileSpec, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(SKIP_ENV_VAR, raising=False)
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: None)

    assert run_onboarding() is False
    assert "`databricks` CLI not on PATH" in capsys.readouterr().out


def test_run_onboarding_is_a_noop_when_everything_is_ready(
    monkeypatch: pytest.MonkeyPatch,
    catalog: tuple[ProfileSpec, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(SKIP_ENV_VAR, raising=False)
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: _CLI)
    _hosts_sequence(monkeypatch, READY)
    monkeypatch.setattr(setup_mod, "_login_profile", lambda *a: pytest.fail("unexpected login"))

    assert run_onboarding() is True
    assert "all profiles ready" in capsys.readouterr().out


def test_run_onboarding_aliases_then_logs_in_remaining_profiles(
    monkeypatch: pytest.MonkeyPatch,
    catalog: tuple[ProfileSpec, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(SKIP_ENV_VAR, raising=False)
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: _CLI)
    aliased: list[tuple[str, str]] = []
    monkeypatch.setattr(setup_mod, "_alias_profile", lambda src, dst: aliased.append((src, dst)))
    logged_in: list[str] = []
    monkeypatch.setattr(
        setup_mod, "_login_profile", lambda cli, spec, console: logged_in.append(spec.name) or True
    )
    _hosts_sequence(
        monkeypatch,
        {"mine": "https://oss.example.com"},  # oss aliasable, jira needs OAuth
        {"mine": "https://oss.example.com", "oss": "https://oss.example.com"},  # after aliasing
        READY,  # after login
    )

    assert run_onboarding() is True

    out = capsys.readouterr().out
    assert aliased == [("mine", "oss")]
    assert logged_in == ["jira"]
    assert "aliased 1 existing profile(s)" in out
    assert "onboarding complete" in out


def test_run_onboarding_reports_profiles_still_missing(
    monkeypatch: pytest.MonkeyPatch,
    catalog: tuple[ProfileSpec, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(SKIP_ENV_VAR, raising=False)
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: _CLI)
    monkeypatch.setattr(setup_mod, "_login_profile", lambda cli, spec, console: False)
    _hosts_sequence(monkeypatch, {})  # nothing ever appears

    assert run_onboarding() is False
    assert "still missing: oss, jira" in capsys.readouterr().out


def test_run_onboarding_resolves_wrong_host_profiles(
    monkeypatch: pytest.MonkeyPatch, catalog: tuple[ProfileSpec, ...]
) -> None:
    monkeypatch.delenv(SKIP_ENV_VAR, raising=False)
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: _CLI)
    handled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        setup_mod,
        "_handle_wrong_host",
        lambda cli, spec, host, console: handled.append((spec.name, host)),
    )
    _hosts_sequence(
        monkeypatch,
        {"oss": "https://old.example.com", "jira": "https://jira.example.com"},
        READY,  # user chose to overwrite
    )

    assert run_onboarding() is True
    assert handled == [("oss", "https://old.example.com")]


# ── maybe_run_onboarding ─────────────────────────────────────


@pytest.fixture()
def interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SKIP_ENV_VAR, raising=False)
    monkeypatch.setattr(setup_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: _CLI)


def test_maybe_run_returns_quietly_when_nothing_to_do(
    monkeypatch: pytest.MonkeyPatch, interactive: None, catalog: tuple[ProfileSpec, ...]
) -> None:
    monkeypatch.setattr(setup_mod, "_existing_profile_hosts", lambda: READY)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("prompted"))

    maybe_run_onboarding()


def test_maybe_run_aliases_silently_without_prompting(
    monkeypatch: pytest.MonkeyPatch,
    interactive: None,
    catalog: tuple[ProfileSpec, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    aliased: list[tuple[str, str]] = []
    monkeypatch.setattr(setup_mod, "_alias_profile", lambda src, dst: aliased.append((src, dst)))
    _hosts_sequence(
        monkeypatch,
        {"mine": "https://oss.example.com", "jira": "https://jira.example.com"},
        READY,
    )
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("prompted"))

    maybe_run_onboarding()

    assert aliased == [("mine", "oss")]
    assert "aliased 1 existing profile(s) to Omnigent names: oss" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["", "y", "YES"])
def test_maybe_run_delegates_to_onboarding_on_consent(
    monkeypatch: pytest.MonkeyPatch,
    interactive: None,
    catalog: tuple[ProfileSpec, ...],
    capsys: pytest.CaptureFixture[str],
    answer: str,
) -> None:
    monkeypatch.setattr(setup_mod, "_existing_profile_hosts", dict)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    ran: list[bool] = []
    monkeypatch.setattr(setup_mod, "run_onboarding", lambda: ran.append(True) or True)

    maybe_run_onboarding()

    assert ran == [True]
    assert "Omnigent needs Databricks profiles: oss, jira" in capsys.readouterr().out


def test_maybe_run_lists_wrong_host_profiles_and_prints_hint_on_decline(
    monkeypatch: pytest.MonkeyPatch,
    interactive: None,
    catalog: tuple[ProfileSpec, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        setup_mod,
        "_existing_profile_hosts",
        lambda: {"oss": "https://old.example.com", "jira": "https://jira.example.com"},
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    monkeypatch.setattr(setup_mod, "run_onboarding", lambda: pytest.fail("ran onboarding"))

    maybe_run_onboarding()

    out = capsys.readouterr().out
    assert "oss (wrong host)" in out
    assert f"{SKIP_ENV_VAR}=1" in out and "setup --internal-beta" in out


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_maybe_run_returns_when_prompt_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    interactive: None,
    catalog: tuple[ProfileSpec, ...],
    exc: type[BaseException],
) -> None:
    monkeypatch.setattr(setup_mod, "_existing_profile_hosts", dict)

    def interrupted(prompt: str) -> str:
        raise exc

    monkeypatch.setattr("builtins.input", interrupted)
    monkeypatch.setattr(setup_mod, "run_onboarding", lambda: pytest.fail("ran onboarding"))

    maybe_run_onboarding()


# ── _existing_profile_hosts failure modes ────────────────────


def _cli_returns(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, stdout: str = ""
) -> None:
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: _CLI)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, returncode, stdout=stdout),
    )


@pytest.mark.parametrize(
    "exc", [OSError("exec failed"), subprocess.TimeoutExpired("databricks", 10)]
)
def test_existing_profile_hosts_is_empty_when_cli_cannot_run(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: _CLI)

    def failing(argv: list[str], **kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(subprocess, "run", failing)

    assert setup_mod._existing_profile_hosts() == {}


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, '{"profiles": [{"name": "a", "host": "h"}]}'),  # CLI reported an error
        (0, "not json"),
        (0, "[]"),  # valid JSON, wrong top-level shape
        (0, '{"profiles": "nope"}'),
    ],
)
def test_existing_profile_hosts_is_empty_for_unusable_cli_output(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    _cli_returns(monkeypatch, returncode=returncode, stdout=stdout)

    assert setup_mod._existing_profile_hosts() == {}


def test_existing_profile_hosts_skips_malformed_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = (
        '{"profiles": [{"name": "ok", "host": "https://ok"}, "junk",'
        ' {"name": "no-host"}, {"name": 3, "host": "https://x"}]}'
    )
    _cli_returns(monkeypatch, stdout=payload)

    assert setup_mod._existing_profile_hosts() == {"ok": "https://ok"}


# ── login failure ────────────────────────────────────────────


def test_login_databricks_workspace_raises_when_login_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click import ClickException

    monkeypatch.setattr(setup_mod, "find_databricks_cli", lambda: _CLI)
    monkeypatch.setattr(setup_mod, "_existing_profile_hosts", dict)
    monkeypatch.setattr(setup_mod, "_login_profile", lambda cli, spec, console: False)

    with pytest.raises(
        ClickException, match=r"`databricks auth login` failed for https://ws\.example\.com"
    ):
        setup_mod.login_databricks_workspace("https://ws.example.com/browse?o=1")
