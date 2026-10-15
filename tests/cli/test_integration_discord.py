"""Tests for ``omni integration discord``.

The daemon manager itself is integration-agnostic and covered by
``test_integration_slack.py``; these pin the Discord wiring — the package the
CLI launches, the install hint, and that the two bots keep separate daemon
records.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.integration_daemon import DaemonRecord, IntegrationDaemon


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate daemon state under a temp OMNIGENT_DATA_DIR."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    return tmp_path


def test_discord_hint_names_its_own_extra_when_not_installed(data_dir: Path) -> None:
    runner = CliRunner()
    with mock.patch("omnigent.cli._discord_installed", return_value=False):
        result = runner.invoke(cli, ["integration", "discord"])
    assert result.exit_code != 0
    assert "isn't installed" in result.output
    assert "omnigent-discord" in result.output
    assert "omnigent[discord]" in result.output


def test_discord_status_reports_not_running(data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["integration", "discord", "status"])
    assert result.exit_code == 0
    assert "Discord bot: not running" in result.output


def test_discord_foreground_runs_the_discord_package(data_dir: Path) -> None:
    runner = CliRunner()
    with (
        mock.patch("omnigent.cli._discord_installed", return_value=True),
        mock.patch("omnigent.cli.subprocess.run") as run,
    ):
        run.return_value = mock.Mock(returncode=0)
        result = runner.invoke(cli, ["integration", "discord"])
    assert result.exit_code == 0
    assert run.call_args.args[0][1:] == ["-m", "omnigent_discord"]


def test_discord_background_lifecycle(data_dir: Path) -> None:
    runner = CliRunner()
    with (
        mock.patch("omnigent.cli._discord_installed", return_value=True),
        mock.patch("omnigent.integration_daemon.subprocess.Popen") as popen,
        mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True),
        mock.patch.object(IntegrationDaemon, "confirm_alive", return_value=True),
    ):
        popen.return_value.pid = 4711
        start = runner.invoke(cli, ["integration", "discord", "--background"])
        assert start.exit_code == 0, start.output
        assert "4711" in start.output
        assert popen.call_args.args[0][1:] == ["-m", "omnigent_discord"]

        status = runner.invoke(cli, ["integration", "discord", "status"])
        assert "Discord bot: running" in status.output and "4711" in status.output

        # --background again is idempotent — reports the existing pid.
        popen.reset_mock()
        again = runner.invoke(cli, ["integration", "discord", "--background"])
        assert "already running" in again.output
        popen.assert_not_called()

    with (
        mock.patch.object(IntegrationDaemon, "_pid_alive", side_effect=[True, False, False]),
        mock.patch.object(IntegrationDaemon, "_signal"),
    ):
        stop = runner.invoke(cli, ["integration", "discord", "stop"])
        assert stop.exit_code == 0
        assert "Stopped the Omnigent Discord bot" in stop.output


def test_discord_foreground_refuses_when_its_daemon_is_running(data_dir: Path) -> None:
    IntegrationDaemon("discord", data_dir)._write_record(
        DaemonRecord(pid=4242, log_path="/tmp/x.log", started_at=1)
    )
    runner = CliRunner()
    with (
        mock.patch("omnigent.cli._discord_installed", return_value=True),
        mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True),
        mock.patch("omnigent.cli.subprocess.run") as run,
    ):
        result = runner.invoke(cli, ["integration", "discord"])
    assert result.exit_code != 0
    assert "already running" in result.output
    run.assert_not_called()


def test_the_two_bots_keep_separate_daemon_records(data_dir: Path) -> None:
    # Running both at once is a normal deployment, so a Discord daemon must not
    # make the Slack CLI think Slack is up (or vice versa).
    IntegrationDaemon("discord", data_dir)._write_record(
        DaemonRecord(pid=4242, log_path="/tmp/discord.log", started_at=1)
    )
    runner = CliRunner()
    with mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True):
        discord_status = runner.invoke(cli, ["integration", "discord", "status"])
        slack_status = runner.invoke(cli, ["integration", "slack", "status"])
    assert "Discord bot: running" in discord_status.output
    assert "Slack bot: not running" in slack_status.output


def test_discord_logs_prints_its_own_path(data_dir: Path) -> None:
    runner = CliRunner()
    none = runner.invoke(cli, ["integration", "discord", "logs"])
    assert "No Discord daemon" in none.output
    IntegrationDaemon("discord", data_dir)._write_record(
        DaemonRecord(pid=1, log_path="/tmp/discord.log", started_at=1)
    )
    result = runner.invoke(cli, ["integration", "discord", "logs"])
    assert result.exit_code == 0
    assert "/tmp/discord.log" in result.output


def test_the_integration_group_lists_both_bots(data_dir: Path) -> None:
    runner = CliRunner()
    output = runner.invoke(cli, ["integration"]).output.lower()
    assert "slack" in output
    assert "discord" in output


# ── running both bots at once ─────────────────────────────────────────────
#
# A team on both Slack and Discord runs both bots against one Omnigent server,
# on one machine, at the same time. Nothing stateful may be shared between
# them, or one bot's session map, tokens, or daemon record would clobber the
# other's.


def test_the_two_bots_use_separate_databases(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent_discord.config import load_settings as discord_settings
    from omnigent_slack.config import load_settings as slack_settings

    for name, value in (
        ("OMNIGENT_DISCORD_BOT_TOKEN", "t"),
        ("OMNIGENT_SLACK_BOT_TOKEN", "xoxb-t"),
        ("OMNIGENT_SLACK_APP_TOKEN", "xapp-t"),
        ("OMNIGENT_SERVER_URL", "https://omnigent.example.com"),
    ):
        monkeypatch.setenv(name, value)
    for name in ("OMNIGENT_DISCORD_DATABASE_PATH", "OMNIGENT_SLACK_DATABASE_PATH"):
        monkeypatch.delenv(name, raising=False)

    assert discord_settings().database_path != slack_settings().database_path


def test_the_two_bots_share_only_server_level_config() -> None:
    # A shared variable is fine when it describes the SERVER both talk to;
    # anything bot-specific sharing a name would make one bot's setup silently
    # reconfigure the other.
    from omnigent_discord.config import Settings as DiscordSettings
    from omnigent_slack.config import Settings as SlackSettings

    def aliases(model: object) -> set[str]:
        return {
            alias
            for field in model.model_fields.values()  # type: ignore[attr-defined]
            if isinstance(alias := getattr(field, "validation_alias", None), str)
        }

    assert aliases(DiscordSettings) & aliases(SlackSettings) == {
        "LOG_LEVEL",
        "OMNIGENT_SERVER_URL",
        "OMNIGENT_DEVICE_CLIENT_SECRET",
    }


def test_both_bots_run_as_independent_daemons(data_dir: Path) -> None:
    runner = CliRunner()
    with (
        mock.patch("omnigent.cli._discord_installed", return_value=True),
        mock.patch("omnigent.cli._slack_installed", return_value=True),
        mock.patch("omnigent.integration_daemon.subprocess.Popen") as popen,
        mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True),
        mock.patch.object(IntegrationDaemon, "confirm_alive", return_value=True),
    ):
        popen.return_value.pid = 100
        assert runner.invoke(cli, ["integration", "slack", "--background"]).exit_code == 0
        popen.return_value.pid = 200
        assert runner.invoke(cli, ["integration", "discord", "--background"]).exit_code == 0

        # Each reports its own pid, and starting one does not displace the other.
        slack_status = runner.invoke(cli, ["integration", "slack", "status"]).output
        discord_status = runner.invoke(cli, ["integration", "discord", "status"]).output
        assert "100" in slack_status and "Slack bot: running" in slack_status
        assert "200" in discord_status and "Discord bot: running" in discord_status

    # Stopping one leaves the other running.
    with (
        mock.patch.object(IntegrationDaemon, "_pid_alive", side_effect=[True, False, False]),
        mock.patch.object(IntegrationDaemon, "_signal"),
    ):
        assert runner.invoke(cli, ["integration", "discord", "stop"]).exit_code == 0
    with mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True):
        still_up = runner.invoke(cli, ["integration", "slack", "status"]).output
    assert "Slack bot: running" in still_up
