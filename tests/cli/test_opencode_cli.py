from click.testing import CliRunner

from omnigent.cli import cli


def test_opencode_in_help():
    result = CliRunner().invoke(cli, ["--help"])
    assert "opencode" in result.output
