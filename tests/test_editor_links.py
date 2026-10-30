"""Tests for omnigent.editor_links (server-built open-in-editor deep links)."""

from __future__ import annotations

import pytest

from omnigent.editor_links import EditorLink, build_editor_links
from omnigent.stores.host_store import Host


def _host(*, sandbox_provider: str | None = None, ssh_target: str | None = None) -> Host:
    return Host(
        host_id="a" * 32,
        name="dev",
        user_id="alice@example.com",
        status="online",
        created_at=0,
        updated_at=0,
        sandbox_provider=sandbox_provider,
        ssh_target=ssh_target,
    )


def _urls(links: list[EditorLink] | None) -> list[str] | None:
    return None if links is None else [link.url for link in links]


@pytest.mark.parametrize(
    ("host", "workspace", "viewer", "expected"),
    [
        pytest.param(
            _host(ssh_target="arclet_dev.ssh"),
            "/home/alice/universe",
            "alice@example.com",
            [
                "cursor://vscode-remote/ssh-remote+arclet_dev.ssh/home/alice/universe",
                "vscode://vscode-remote/ssh-remote+arclet_dev.ssh/home/alice/universe",
            ],
            id="remote-host-with-ssh-target",
        ),
        pytest.param(
            _host(ssh_target="arclet_dev.ssh", sandbox_provider="arclet"),
            "/home/alice/universe",
            "alice@example.com",
            [
                "cursor://vscode-remote/ssh-remote+arclet_dev.ssh/home/alice/universe",
                "vscode://vscode-remote/ssh-remote+arclet_dev.ssh/home/alice/universe",
            ],
            id="managed-host-that-reports-a-target",
        ),
        pytest.param(
            _host(),
            "/Users/alice/repo",
            "alice@example.com",
            ["cursor://file/Users/alice/repo", "vscode://file/Users/alice/repo"],
            id="plain-host-gets-local-links",
        ),
        pytest.param(
            _host(sandbox_provider="modal"),
            "/workspace",
            "alice@example.com",
            None,
            id="managed-sandbox-without-target",
        ),
        pytest.param(
            _host(ssh_target="arca.ssh"),
            "/home/alice",
            "bob@example.com",
            None,
            id="viewer-is-not-the-host-owner",
        ),
        pytest.param(
            _host(ssh_target="arca.ssh"), "/home/alice", None, None, id="anonymous-viewer"
        ),
        pytest.param(None, "/home/alice", "alice@example.com", None, id="no-host"),
        pytest.param(
            _host(ssh_target="arca.ssh"), None, "alice@example.com", None, id="no-workspace"
        ),
        pytest.param(
            _host(ssh_target="arca.ssh"),
            "relative/path",
            "alice@example.com",
            None,
            id="relative-workspace",
        ),
    ],
)
def test_build_editor_links(host, workspace, viewer, expected) -> None:
    assert _urls(build_editor_links(workspace=workspace, host=host, viewer_id=viewer)) == expected


def test_links_list_cursor_first_and_flag_local_links() -> None:
    remote = build_editor_links(
        workspace="/w", host=_host(ssh_target="arca.ssh"), viewer_id="alice@example.com"
    )
    local = build_editor_links(workspace="/w", host=_host(), viewer_id="alice@example.com")
    assert remote is not None and local is not None
    assert [(link.editor, link.label) for link in remote] == [
        ("cursor", "Cursor"),
        ("vscode", "VS Code"),
    ]
    assert not any(link.local_only for link in remote)
    assert all(link.local_only for link in local)


def test_workspace_path_is_percent_encoded_but_keeps_slashes() -> None:
    links = build_editor_links(
        workspace="/home/alice/my repo/#1?x=%/naïve",
        host=_host(ssh_target="arca.ssh"),
        viewer_id="alice@example.com",
    )
    assert links is not None
    assert links[0].url == (
        "cursor://vscode-remote/ssh-remote+arca.ssh/home/alice/my%20repo/%231%3Fx%3D%25/na%C3%AFve"
    )
