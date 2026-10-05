"""``omnigent agent add|list|remove`` against a mocked ``/v1/agents``."""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import click
import httpx
import pytest
from click.testing import CliRunner

from omnigent import cli as cli_mod


class _FakeServer:
    """In-memory ``/v1/info`` + ``/v1/agents`` behind the real CLI client."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.agent_install = True
        self.sessions_in_use: str | None = None
        # The caller's own agents (GET /v1/agents?scope=user), one page per entry.
        self.pages = [
            {
                "data": [{"id": "ag_orion", "name": "orion", "version": 2, "harness": "codex"}],
                "has_more": True,
                "last_id": "ag_row50",
            },
            {
                "data": [{"id": "ag_vega", "name": "vega", "version": 1}],
                "has_more": False,
                "last_id": "ag_vega",
            },
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.url.path == "/v1/info":
            return httpx.Response(
                200, json={"agent_install": self.agent_install} if self.agent_install else {}
            )
        if request.method == "POST":
            return httpx.Response(200, json={"id": "ag_orion", "name": "orion", "version": 3})
        if request.method == "DELETE":
            if self.sessions_in_use and request.url.params.get("force") != "true":
                return httpx.Response(
                    409,
                    json={
                        "error": {"code": "agent_in_use"},
                        "sessions_in_use": self.sessions_in_use,
                    },
                )
            return httpx.Response(200, json={"id": "ag_orion", "deleted": True})
        assert request.url.params.get("scope") == "user", "only the caller's agents are listed"
        page = 1 if request.url.params.get("after") == "ag_row50" else 0
        return httpx.Response(200, json=self.pages[page])

    def calls(self) -> list[tuple[str, str, str]]:
        return [(r.method, r.url.path, r.url.query.decode()) for r in self.seen]


@pytest.fixture()
def server(monkeypatch: pytest.MonkeyPatch) -> _FakeServer:
    """Route the CLI's real API client to :class:`_FakeServer`."""
    from omnigent.util.server_url import ServerUrl

    fake = _FakeServer()
    real_client = httpx.Client
    monkeypatch.setattr(
        cli_mod, "_resolve_attach_server_url", lambda server, configured: ServerUrl("http://t")
    )
    monkeypatch.setattr("omnigent.chat._remote_headers", lambda **kwargs: {})
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(fake), base_url=kwargs["base_url"]
        ),
    )
    return fake


def test_add_uploads_the_whole_directory(tmp_path: Path, server: _FakeServer) -> None:
    (tmp_path / "config.yaml").write_text("spec_version: 1\nname: orion\n")
    (tmp_path / "agents" / "worker").mkdir(parents=True)
    (tmp_path / "agents" / "worker" / "config.yaml").write_text("name: worker\n")

    result = CliRunner().invoke(cli_mod.cli, ["agent", "add", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Installed orion (version 3" in result.output
    assert "terminal sessions do when they relaunch" in result.output
    post = next(r for r in server.seen if r.method == "POST")
    body = post.read()
    boundary = post.headers["content-type"].split("boundary=")[1].encode()
    start = body.index(b"\x1f\x8b")  # the gzip part of the multipart body
    tar_bytes = body[start : body.index(b"\r\n--" + boundary, start)]
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(tar_bytes)), mode="r:") as tf:
        names = {n.lstrip("./") for n in tf.getnames()}
    assert {"config.yaml", "agents/worker/config.yaml"} <= names


def test_list_pages_through_your_agents(server: _FakeServer) -> None:
    result = CliRunner().invoke(cli_mod.cli, ["agent", "list"])
    assert result.exit_code == 0, result.output
    rows = [line.split() for line in result.output.splitlines()]
    header = rows.index(["Name", "Version", "Harness", "ID"])
    assert [row for row in rows[header + 1 :] if len(row) == 4] == [
        ["orion", "2", "codex", "ag_orion"],
        ["vega", "1", "-", "ag_vega"],
    ]
    assert [q for m, _, q in server.calls() if m == "GET" and "scope" in q] == [
        "scope=user&limit=50",
        "scope=user&limit=50&after=ag_row50",
    ]


@pytest.mark.parametrize("configured", [None, "local"])
def test_server_local_means_the_local_server(
    server: _FakeServer, monkeypatch: pytest.MonkeyPatch, configured: str | None
) -> None:
    """``local`` (the flag, or the configured default) is the local server, as for
    ``omnigent run``, not a remote URL to reject."""
    from types import SimpleNamespace

    def refuse(_server: str | None, _configured: object) -> None:
        raise AssertionError("'local' must not be resolved as a remote URL")

    monkeypatch.setattr(cli_mod, "_resolve_attach_server_url", refuse)
    monkeypatch.setattr(cli_mod, "_load_effective_config", lambda: {"server": configured})
    monkeypatch.setattr(
        cli_mod,
        "ensure_local_omnigent_server",
        lambda: SimpleNamespace(url="http://127.0.0.1:6767"),
    )
    args = ["agent", "list"] + (["--server", "local"] if configured is None else [])
    result = CliRunner().invoke(cli_mod.cli, args)
    assert result.exit_code == 0, result.output
    assert {r.url.host for r in server.seen} == {"127.0.0.1"}


def test_list_without_agents_says_how_to_add_one(server: _FakeServer) -> None:
    server.pages = [{"data": [], "has_more": False, "last_id": None}]
    result = CliRunner().invoke(cli_mod.cli, ["agent", "list"])
    assert result.exit_code == 0, result.output
    assert "No agents yet. Install one with: omnigent agent add <path>" in result.output


def test_agent_is_a_registered_subcommand() -> None:
    assert "agent" in cli_mod._CLICK_SUBCOMMANDS


def test_remove_deletes_by_id(server: _FakeServer) -> None:
    result = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "orion"])
    assert result.exit_code == 0, result.output
    assert server.calls()[-1] == ("DELETE", "/v1/agents/ag_orion", "")


def test_remove_refuses_an_ambiguous_name_and_takes_an_id(server: _FakeServer) -> None:
    server.pages[1]["data"].append({"id": "ag_orion_old", "name": "orion", "version": 1})

    ambiguous = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "orion"])
    assert ambiguous.exit_code != 0
    assert "2 agents named 'orion'; remove one by id: ag_orion, ag_orion_old" in ambiguous.output
    assert not any(m == "DELETE" for m, _, _ in server.calls())

    by_id = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "ag_orion_old"])
    assert by_id.exit_code == 0, by_id.output
    assert server.calls()[-1] == ("DELETE", "/v1/agents/ag_orion_old", "")


def test_remove_unknown_name_fails(server: _FakeServer) -> None:
    result = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "polly"])
    assert result.exit_code != 0
    assert "You have no agent named 'polly'" in result.output


def test_remove_in_use_asks_before_breaking_sessions(server: _FakeServer) -> None:
    server.sessions_in_use = "2"
    declined = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "orion"], input="n\n")
    assert declined.exit_code != 0
    assert "2 session(s) still use orion; removing it will break them" in declined.output
    assert not any("force=true" in q for _, _, q in server.calls())

    accepted = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "orion"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert server.calls()[-1] == ("DELETE", "/v1/agents/ag_orion", "force=true")


def test_remove_yes_skips_the_prompt(server: _FakeServer) -> None:
    server.sessions_in_use = "2"
    result = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "orion", "--yes"])
    assert result.exit_code == 0, result.output
    assert server.calls()[-1] == ("DELETE", "/v1/agents/ag_orion", "force=true")


@pytest.mark.parametrize("args", [["add"], ["list"], ["remove", "orion"]])
def test_older_server_is_reported_not_treated_as_empty(
    server: _FakeServer, args: list[str], tmp_path: Path
) -> None:
    server.agent_install = False
    if args == ["add"]:
        (tmp_path / "config.yaml").write_text("spec_version: 1\nname: orion\n")
        args = ["add", str(tmp_path)]
    result = CliRunner().invoke(cli_mod.cli, ["agent", *args])
    assert result.exit_code != 0
    assert "does not support installing agents" in result.output
    assert [path for _, path, _ in server.calls()] == ["/v1/info"]


def test_agent_errors_skip_the_stale_host_hint(server: _FakeServer) -> None:
    """A 409 or unknown name has nothing to do with stale host processes."""
    from omnigent.cli_diagnostics import suppresses_recovery_hint

    result = CliRunner().invoke(cli_mod.cli, ["agent", "remove", "polly"], standalone_mode=False)
    assert isinstance(result.exception, click.ClickException)
    assert suppresses_recovery_hint(result.exception)
