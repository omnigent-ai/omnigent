"""Tests for the local health section of the ``omnigent diagnose`` snapshot."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent import diagnostics_health
from omnigent.diagnostics_health import collect_health

_WORKSPACE = "https://example.cloud.databricks.com"
_SERVER = f"{_WORKSPACE}/api/2.0/omnigent"


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the logs root at a scratch dir so probes never read the real one."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))


def _write_log(tmp_path: Path, destination: str, name: str, body: str) -> Path:
    directory = tmp_path / "logs" / destination
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


# --- credential ------------------------------------------------------------


def test_credential_not_applicable_without_server() -> None:
    health = collect_health(server_url=None)
    assert health["credential"]["probe"] == "not-applicable"
    assert health["credential"]["profiles_matching_host"] is None


def test_credential_not_applicable_for_non_databricks_server() -> None:
    # An OSS/self-hosted server has no workspace profiles to count.
    health = collect_health(server_url="http://localhost:6767")
    assert health["credential"] == {
        "backend": "other",
        "profiles_matching_host": None,
        "probe": "not-applicable",
    }


@pytest.mark.parametrize("matches", [[], ["only-one"], ["first", "second"], ["a", "b", "c"]])
def test_credential_reports_the_match_count(
    monkeypatch: pytest.MonkeyPatch, matches: list[str]
) -> None:
    monkeypatch.setattr(diagnostics_health, "_profiles_for_host", lambda host: matches)
    credential = collect_health(server_url=_SERVER)["credential"]
    assert credential["profiles_matching_host"] == len(matches)
    assert credential["backend"] == "databricks"
    # ``probe`` describes the probe, never the credential: it stays "ok" for
    # every count, including 0 and 2+.
    assert credential["probe"] == "ok"


def test_credential_never_turns_the_count_into_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A regression guard against re-introducing a verdict field.

    ``_resolve_databricks_auth_for_host`` authenticates each matching profile in
    turn and falls back to a host-keyed lookup, so no count implies success or
    failure: 2+ matches is not a failure, 1 match is not success, and 0 matches is
    not "unauthenticated".
    """
    monkeypatch.setattr(diagnostics_health, "_profiles_for_host", lambda host: ["a", "b"])
    credential = collect_health(server_url=_SERVER)["credential"]
    assert set(credential) == {"backend", "profiles_matching_host", "probe"}
    verdicts = {"ambiguous", "unauthenticated", "authenticated", "broken", "ok"}
    assert not (verdicts - {"ok"}) & set(map(str, credential.values()))


def test_credential_probe_uses_the_workspace_host_not_the_api_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Credentials are keyed by the workspace host; the Omnigent server sits on
    # a path under it, so the path must be stripped before matching.
    seen: list[str] = []

    def _record(host: str) -> list[str]:
        seen.append(host)
        return ["profile"]

    monkeypatch.setattr(diagnostics_health, "_profiles_for_host", _record)
    collect_health(server_url=f"{_SERVER}?o=123#frag")
    assert seen == [_WORKSPACE]


def test_credential_unavailable_when_matcher_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # No databricks extra installed, or an unreadable config: degrade, never raise.
    def _boom(host: str) -> list[str]:
        raise RuntimeError("no databricks extra")

    monkeypatch.setattr(diagnostics_health, "_profiles_for_host", _boom)
    credential = collect_health(server_url=_SERVER)["credential"]
    assert credential["probe"] == "unavailable"
    assert credential["profiles_matching_host"] is None


# --- host log --------------------------------------------------------------


def test_host_log_absent_reports_null() -> None:
    assert collect_health(server_url=None)["host_log"] is None


def test_host_log_stalled_mid_connect(tmp_path: Path) -> None:
    # The fingerprint of a server-side tunnel refusal: the connect line is the
    # last thing written and nothing follows it.
    _write_log(
        tmp_path,
        "host",
        "host-1.log",
        "INFO host.connect run | Connecting to wss://example/api/2.0/omnigent/v1/host\n",
    )
    host_log = collect_health(server_url=None)["host_log"]
    assert host_log is not None
    assert host_log["stalled_on_connect"] is True
    assert host_log["service_restarts"] == 0
    assert host_log["size_bytes"] > 0


def test_host_log_not_stalled_and_counts_service_restarts(tmp_path: Path) -> None:
    _write_log(
        tmp_path,
        "host",
        "host-1.log",
        "INFO host.connect run | Connecting to wss://example\n"
        "WARN host.connect run | Host tunnel disconnected: received 1012 (service restart)\n"
        "WARN host.connect run | Host tunnel disconnected: received 1012 (service restart)\n"
        "INFO host.connect _handle_launch | Launched runner\n",
    )
    host_log = collect_health(server_url=None)["host_log"]
    assert host_log is not None
    # False means only "the log does not end on the connect line" — never
    # "the tunnel is up".
    assert host_log["stalled_on_connect"] is False
    assert host_log["service_restarts"] == 2


def test_host_log_picks_the_newest(tmp_path: Path) -> None:
    old = _write_log(tmp_path, "host", "host-old.log", "Connecting to wss://example\n")
    new = _write_log(tmp_path, "host", "host-new.log", "INFO settled\n")
    import os

    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    host_log = collect_health(server_url=None)["host_log"]
    assert host_log is not None
    # The newest log is not stalled; picking the older one would say otherwise.
    assert host_log["stalled_on_connect"] is False


# --- runner log ------------------------------------------------------------


def test_runner_log_absent_reports_null() -> None:
    assert collect_health(server_url=None)["runner_log"] is None


def test_runner_log_healthy_self_recovery(tmp_path: Path) -> None:
    # The healthy shape: the launch bearer lapsed, and a self-refreshing
    # credential took over. No empty refreshes.
    _write_log(
        tmp_path,
        "runner",
        "runner-1.log",
        "INFO __main__ | using host-provided bearer for runner bootstrap\n"
        "INFO runner._entry invalidate | host bootstrap bearer rejected;"
        " resolving runner-local auth\n"
        "INFO databricks.sdk databricks_cli | Using Databricks CLI authentication\n",
    )
    runner_log = collect_health(server_url=None)["runner_log"]
    assert runner_log is not None
    assert runner_log["bearer_handoff"] is True
    assert runner_log["bearer_rejected"] == 1
    assert runner_log["sdk_fallback"] == 1
    assert runner_log["refresh_empty"] == 0


def test_runner_log_stuck_without_a_credential(tmp_path: Path) -> None:
    # The stuck shape: refreshes keep producing nothing and no fallback lands.
    _write_log(
        tmp_path,
        "runner",
        "runner-1.log",
        "INFO __main__ | using host-provided bearer for runner bootstrap\n"
        + "httpx.RequestError: Databricks token refresh returned no token\n" * 3,
    )
    runner_log = collect_health(server_url=None)["runner_log"]
    assert runner_log is not None
    assert runner_log["refresh_empty"] == 3
    assert runner_log["sdk_fallback"] == 0


def test_runner_log_includes_host_launched_runners(tmp_path: Path) -> None:
    # Host-launched runners log under a different destination directory.
    _write_log(
        tmp_path,
        "host-runner",
        "runner-1.log",
        "INFO __main__ | using host-provided bearer for runner bootstrap\n",
    )
    runner_log = collect_health(server_url=None)["runner_log"]
    assert runner_log is not None
    assert runner_log["bearer_handoff"] is True


def test_runner_log_startup_marker_survives_a_long_lived_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regression guard: the launch marker must not scroll out of view.

    ``using host-provided bearer`` is logged once, in the first few lines. A
    tail-only read reports "no bearer from host" for every session whose log has
    since grown past the tail window — which is every long-lived session.
    """
    monkeypatch.setattr(diagnostics_health, "_LOG_TAIL_BYTES", 128)
    _write_log(
        tmp_path,
        "runner",
        "runner-1.log",
        "using host-provided bearer for runner bootstrap\n" + ("x" * 8192) + "\n",
    )
    runner_log = collect_health(server_url=None)["runner_log"]
    assert runner_log is not None
    assert runner_log["bearer_handoff"] is True
    assert runner_log["size_bytes"] > 128  # the true size is still reported


def test_runner_log_short_file_is_entirely_the_recent_window(tmp_path: Path) -> None:
    # A short log has no separate tail; the whole file is the recent window, and
    # a marker in it must be counted exactly once.
    _write_log(
        tmp_path,
        "runner",
        "runner-1.log",
        "INFO databricks.sdk databricks_cli | Using Databricks CLI authentication\n"
        "INFO runner.app | ready\n",
    )
    runner_log = collect_health(server_url=None)["runner_log"]
    assert runner_log is not None
    assert runner_log["sdk_fallback"] == 1


def test_runner_log_counters_describe_the_recent_window_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old recovery must not make a currently-stuck runner read as healthy.

    Observed on a real log: a runner lapsed and recovered early in the session,
    then went stuck hours later. Counting the whole file would pair the old
    recovery with the current failures and hide the stuck state.
    """
    monkeypatch.setattr(diagnostics_health, "_LOG_HEAD_BYTES", 64)
    monkeypatch.setattr(diagnostics_health, "_LOG_TAIL_BYTES", 64)
    _write_log(
        tmp_path,
        "runner",
        "runner-1.log",
        "using host-provided bearer for runner bootstrap\n"
        "INFO databricks.sdk databricks_cli | Using Databricks CLI authentication\n"
        + ("x" * 4096)
        + "\nDatabricks token refresh returned no token\n",
    )
    runner_log = collect_health(server_url=None)["runner_log"]
    assert runner_log is not None
    assert runner_log["bearer_handoff"] is True  # still found, from the head
    assert runner_log["sdk_fallback"] == 0  # the old recovery is not recent
    assert runner_log["refresh_empty"] == 1  # currently failing


def test_runner_log_head_read_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The head window is bounded too: a marker beyond it is not a startup fact.
    monkeypatch.setattr(diagnostics_health, "_LOG_HEAD_BYTES", 32)
    monkeypatch.setattr(diagnostics_health, "_LOG_TAIL_BYTES", 32)
    _write_log(
        tmp_path,
        "runner",
        "runner-1.log",
        ("y" * 4096) + "\nusing host-provided bearer for runner bootstrap\n" + ("z" * 4096) + "\n",
    )
    runner_log = collect_health(server_url=None)["runner_log"]
    assert runner_log is not None
    assert runner_log["bearer_handoff"] is False


# --- shape / secrets -------------------------------------------------------


def test_health_reports_only_known_sections() -> None:
    assert set(collect_health(server_url=None)) == {"credential", "host_log", "runner_log"}


def test_health_contains_no_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Profile names and log bodies must not leak into a snapshot meant for a
    # bug report — only counts, booleans, and fixed classification strings.
    monkeypatch.setattr(
        diagnostics_health, "_profiles_for_host", lambda host: ["secret-profile-name"]
    )
    _write_log(
        tmp_path,
        "runner",
        "runner-1.log",
        # A synthetic bearer, assembled so this file carries no JWT-shaped literal.
        "Authorization: Bearer " + ("ey" + "J-NOT-A-REAL-TOKEN-") * 2 + "\n",
    )
    blob = repr(collect_health(server_url=_SERVER))
    assert "secret-profile-name" not in blob
    assert "Bearer" not in blob
    assert "NOT-A-REAL-TOKEN" not in blob
    assert "example.cloud.databricks.com" not in blob


def test_collect_health_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A half-broken machine is exactly when a snapshot is collected.
    monkeypatch.setenv("OMNIGENT_DATA_DIR", "/nonexistent/path/for/diagnostics/test")

    def _boom(host: str) -> list[str]:
        raise OSError("permission denied")

    monkeypatch.setattr(diagnostics_health, "_profiles_for_host", _boom)
    health = collect_health(server_url=_SERVER)
    assert health["credential"]["probe"] == "unavailable"
    assert health["host_log"] is None
    assert health["runner_log"] is None


# --- facts reported alongside every log section ------------------------------


def test_log_sections_report_the_window_and_idle_time(tmp_path: Path) -> None:
    """``window_bytes`` bounds the counters; ``idle_seconds`` dates them.

    Without the window, a count reads as a lifetime total. Without the idle time,
    ``stalled_on_connect`` cannot be told apart from a connect in flight.
    """
    _write_log(tmp_path, "host", "host-1.log", "Connecting to wss://example\n")
    _write_log(tmp_path, "runner", "runner-1.log", "using host-provided bearer\n")
    health = collect_health(server_url=None)
    for section in (health["host_log"], health["runner_log"]):
        assert section is not None
        assert section["window_bytes"] == section["size_bytes"]  # short file: read whole
        assert section["idle_seconds"] >= 0


def test_idle_seconds_grows_with_an_old_log(tmp_path: Path) -> None:
    import os

    path = _write_log(tmp_path, "host", "host-1.log", "Connecting to wss://example\n")
    os.utime(path, (1_000_000, 1_000_000))  # long in the past
    host_log = collect_health(server_url=None)["host_log"]
    assert host_log is not None
    # Stalled *and* idle for ages is the conclusive shape; stalled at ~0s is not.
    assert host_log["stalled_on_connect"] is True
    assert host_log["idle_seconds"] > 10_000


# --- robustness --------------------------------------------------------------


def test_credential_section_degrades_when_the_probe_itself_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not just the matcher: any failure inside the credential probe must cost a
    # null section rather than the whole snapshot.
    def _boom(server_url: str | None) -> bool:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(diagnostics_health, "_is_databricks_host", _boom)
    health = collect_health(server_url=_SERVER)
    assert health["credential"] is None
    assert set(health) == {"credential", "host_log", "runner_log"}


def test_log_windows_tolerate_a_split_multibyte_character(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The windows are byte offsets, so a boundary can land inside a UTF-8
    # sequence. Decoding must replace, not raise, and markers must still match.
    monkeypatch.setattr(diagnostics_health, "_LOG_HEAD_BYTES", 5)
    monkeypatch.setattr(diagnostics_health, "_LOG_TAIL_BYTES", 5)
    path = tmp_path / "logs" / "runner" / "runner-1.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    # "あ" is three bytes; the 5-byte windows cut through the sequences.
    path.write_bytes(("あ" * 40).encode("utf-8") + b"\nno token\n")
    runner_log = collect_health(server_url=None)["runner_log"]
    assert runner_log is not None
    assert runner_log["refresh_empty"] == 0  # the full marker is not in the window


def test_log_section_survives_a_file_that_vanishes_mid_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _newest_log finds it, then it is gone before the read: null, not a crash.
    path = _write_log(tmp_path, "runner", "runner-1.log", "using host-provided bearer\n")
    real_windows = diagnostics_health._log_windows

    def _unlink_then_read(p: Path) -> tuple[str, str]:
        path.unlink(missing_ok=True)
        return real_windows(p)

    monkeypatch.setattr(diagnostics_health, "_log_windows", _unlink_then_read)
    runner_log = collect_health(server_url=None)["runner_log"]
    assert runner_log is None or runner_log["size_bytes"] == 0
