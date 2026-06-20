"""Tests for the native Claude Code hook command."""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

from omnigent import claude_native_hook
from omnigent.claude_native_bridge import (
    build_hook_settings,
    prepare_bridge_dir,
    read_transcript_path,
    record_hook_event,
    write_active_session_id,
)
from tests.native_hook_helpers import make_failing_client


@pytest.fixture(autouse=True)
def _trust_tmp_bridge_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Treat each test's temp dir as the Claude bridge root.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp directory.
    :returns: None.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path)


def test_session_start_hook_records_transcript_state_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    SessionStart records Claude state without printing hook output.

    This fails if the ``omnigent claude`` hook reintroduces
    ``systemMessage`` output, which Claude renders with the noisy
    ``SessionStart:startup says:`` prefix.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    payload = {
        "hook_event_name": "SessionStart",
        "transcript_path": str(transcript_path),
    }
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(
        [
            "--bridge-dir",
            str(bridge_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""
    assert read_transcript_path(bridge_dir) == transcript_path


def test_session_start_hook_emits_conversation_url_system_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    SessionStart emits Claude hook output when a conversation URL exists.

    This fails if ``omnigent claude`` stops routing the web URL
    through Claude's hook output path, leaving users with no startup
    pointer back to the Omnigent conversation.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = prepare_bridge_dir(
        "conv_abc",
        bridge_id="bridge_shared",
        workspace=tmp_path,
    )
    transcript_path = tmp_path / "session.jsonl"
    build_hook_settings(bridge_dir, ap_server_url="http://127.0.0.1:8787")
    payload = {
        "hook_event_name": "SessionStart",
        "transcript_path": str(transcript_path),
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(
        [
            "--bridge-dir",
            str(bridge_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {
        "systemMessage": "Open this session in Omnigent: http://127.0.0.1:8787/c/conv_abc"
    }
    assert captured.err == ""
    assert read_transcript_path(bridge_dir) == transcript_path


def test_session_start_hook_maps_workspace_hosted_server_to_ui_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    SessionStart links to the SPA mount for workspace-hosted servers.

    ``ap_server_url`` is the API proxy base (``/api/2.0/omnigent``);
    pointing the "Open this session" message there returns JSON, not
    the web UI. The message must land on the ``/omnigent`` SPA mount
    with the ``?o=<org>`` selector — matching the CLI's ``Web UI:``
    line and the tmux status bar.
    """
    from omnigent.cli_auth import store_databricks_auth

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path",
        lambda: tmp_path / "auth_tokens.json",
    )
    server = "https://example.databricks.com/api/2.0/omnigent"
    store_databricks_auth(
        server,
        "https://example.databricks.com",
        org_id="2850744067564480",
    )
    bridge_dir = prepare_bridge_dir(
        "conv_abc",
        bridge_id="bridge_shared",
        workspace=tmp_path,
    )
    build_hook_settings(bridge_dir, ap_server_url=server)
    payload = {
        "hook_event_name": "SessionStart",
        "transcript_path": str(tmp_path / "session.jsonl"),
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(["--bridge-dir", str(bridge_dir)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {
        "systemMessage": (
            "Open this session in Omnigent: "
            "https://example.databricks.com/omnigent/c/conv_abc?o=2850744067564480"
        )
    }


def test_clear_session_start_hook_rotates_before_printing_conversation_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    ``/clear`` SessionStart prints the URL for the replacement Omnigent session.

    Claude renders hook stdout immediately, before the background
    forwarder can poll the hook log. This test fails if the banner
    regresses to the launch conversation URL after ``/clear``.
    """
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    class _FakeHttpxClient:
        """
        Minimal sync HTTP client stub for clear-session rotation.

        :param headers: Headers passed to :class:`httpx.Client`.
        :param timeout: Timeout passed to :class:`httpx.Client`.
        """

        captured_timeouts: list[object] = []

        def __init__(self, *, headers: dict[str, str], timeout: object) -> None:
            """
            Capture constructor inputs.

            :param headers: HTTP headers for AP.
            :param timeout: HTTP timeout object.
            :returns: None.
            """
            del headers
            self.captured_timeouts.append(timeout)

        def __enter__(self) -> _FakeHttpxClient:
            """
            Enter the context manager.

            :returns: This fake client.
            """
            return self

        def __exit__(self, *args: object) -> None:
            """
            Exit the context manager.

            :param args: Exception details.
            :returns: None.
            """
            del args

        def get(self, url: str) -> object:
            """
            Return the old session snapshot.

            :param url: Target Omnigent URL.
            :returns: HTTP response object.
            """
            import httpx

            requests.append(("GET", url, None))
            return httpx.Response(
                200,
                json={
                    "id": "conv_old",
                    "agent_id": "ag_claude",
                    "runner_id": "runner_one",
                    "labels": {"omnigent.claude_native.bridge_id": "bridge_shared"},
                },
                request=httpx.Request("GET", url),
            )

        def post(self, url: str, *, json: dict[str, object]) -> object:
            """
            Create the replacement session or transfer the terminal.

            :param url: Target Omnigent URL.
            :param json: Request JSON body.
            :returns: HTTP response object.
            """
            import httpx

            requests.append(("POST", url, json))
            if url == "http://127.0.0.1:8787/v1/sessions":
                return httpx.Response(
                    201,
                    json={"id": "conv_new"},
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200,
                json={"id": "terminal_claude_main"},
                request=httpx.Request("POST", url),
            )

        def patch(self, url: str, *, json: dict[str, object]) -> object:
            """
            Bind the new session or clear the old runner binding.

            :param url: Target Omnigent URL.
            :param json: Request JSON body.
            :returns: HTTP response object.
            """
            import httpx

            requests.append(("PATCH", url, json))
            return httpx.Response(
                200,
                json={"id": "patched"},
                request=httpx.Request("PATCH", url),
            )

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setattr(claude_native_hook.httpx, "Client", _FakeHttpxClient)
    bridge_dir = prepare_bridge_dir(
        "conv_old",
        bridge_id="bridge_shared",
        workspace=tmp_path,
    )
    build_hook_settings(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        ap_auth_headers={"Authorization": "Bearer xyz"},
    )
    payload = {
        "hook_event_name": "SessionStart",
        "source": "clear",
        "transcript_path": str(tmp_path / "session.jsonl"),
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(["--bridge-dir", str(bridge_dir)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {
        "systemMessage": "Open this session in Omnigent: http://127.0.0.1:8787/c/conv_new"
    }
    assert captured.err == ""
    assert requests == [
        ("GET", "http://127.0.0.1:8787/v1/sessions/conv_old", None),
        (
            "POST",
            "http://127.0.0.1:8787/v1/sessions",
            {
                "agent_id": "ag_claude",
                "labels": {"omnigent.claude_native.bridge_id": "bridge_shared"},
            },
        ),
        ("PATCH", "http://127.0.0.1:8787/v1/sessions/conv_new", {"runner_id": "runner_one"}),
        (
            "POST",
            (
                "http://127.0.0.1:8787/v1/sessions/conv_old/resources/"
                "terminals/terminal_claude_main/transfer"
            ),
            {"target_session_id": "conv_new"},
        ),
        ("PATCH", "http://127.0.0.1:8787/v1/sessions/conv_old", {"runner_id": ""}),
    ]
    recorded = (bridge_dir / "hooks.jsonl").read_text(encoding="utf-8")
    assert '"omnigent_clear_rotated_to":"conv_new"' in recorded
    # The /clear rotation gates Claude's welcome banner and must fail
    # fast — it uses _SESSION_ROTATION_TIMEOUT_S, NOT the day-long
    # permission long-poll budget. If this regresses to
    # _PERMISSION_TIMEOUT_S (86400) an unresponsive Omnigent server would hang
    # the banner for a full day instead of returning None so the
    # background forwarder can rotate.
    rotation_timeout = _FakeHttpxClient.captured_timeouts[0]
    assert isinstance(rotation_timeout, httpx.Timeout)
    assert rotation_timeout.read == claude_native_hook._SESSION_ROTATION_TIMEOUT_S


def test_fork_session_start_hook_forks_before_printing_conversation_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Claude ``/fork`` SessionStart prints the URL for the forked Omnigent session.

    Claude reports ``/fork``/``/branch`` as ``SessionStart`` with
    ``source=resume``. This test fails if the hook no longer detects
    the branch marker, forks AP, transfers the terminal, and points the
    welcome banner at the forked session.
    """
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    class _FakeHttpxClient:
        """
        Minimal sync HTTP client stub for fork-session rotation.

        :param headers: Headers passed to :class:`httpx.Client`.
        :param timeout: Timeout passed to :class:`httpx.Client`.
        """

        captured_timeouts: list[object] = []

        def __init__(self, *, headers: dict[str, str], timeout: object) -> None:
            """
            Capture constructor inputs.

            :param headers: HTTP headers for AP.
            :param timeout: HTTP timeout object.
            :returns: None.
            """
            del headers
            self.captured_timeouts.append(timeout)

        def __enter__(self) -> _FakeHttpxClient:
            """
            Enter the context manager.

            :returns: This fake client.
            """
            return self

        def __exit__(self, *args: object) -> None:
            """
            Exit the context manager.

            :param args: Exception details.
            :returns: None.
            """
            del args

        def get(self, url: str) -> object:
            """
            Return the old session snapshot.

            :param url: Target Omnigent URL.
            :returns: HTTP response object.
            """
            import httpx

            requests.append(("GET", url, None))
            return httpx.Response(
                200,
                json={
                    "id": "conv_old",
                    "agent_id": "ag_claude",
                    "runner_id": "runner_one",
                    "labels": {"omnigent.claude_native.bridge_id": "bridge_shared"},
                },
                request=httpx.Request("GET", url),
            )

        def post(self, url: str, *, json: dict[str, object]) -> object:
            """
            Fork the Omnigent session or transfer the terminal.

            :param url: Target Omnigent URL.
            :param json: Request JSON body.
            :returns: HTTP response object.
            """
            import httpx

            requests.append(("POST", url, json))
            if url == "http://127.0.0.1:8787/v1/sessions/conv_old/fork":
                return httpx.Response(
                    201,
                    json={"id": "conv_fork"},
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200,
                json={"id": "terminal_claude_main"},
                request=httpx.Request("POST", url),
            )

        def patch(self, url: str, *, json: dict[str, object]) -> object:
            """
            Bind the forked session or clear the old runner binding.

            :param url: Target Omnigent URL.
            :param json: Request JSON body.
            :returns: HTTP response object.
            """
            import httpx

            requests.append(("PATCH", url, json))
            return httpx.Response(
                200,
                json={"id": "patched"},
                request=httpx.Request("PATCH", url),
            )

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setattr(claude_native_hook.httpx, "Client", _FakeHttpxClient)
    bridge_dir = prepare_bridge_dir(
        "conv_old",
        bridge_id="bridge_shared",
        workspace=tmp_path,
    )
    build_hook_settings(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        ap_auth_headers={"Authorization": "Bearer xyz"},
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude_old",
        },
    )
    transcript_path = tmp_path / "fork.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "attachment",
                "timestamp": "2026-05-27T22:53:13.245Z",
                "sessionId": "claude_fork",
                "forkedFrom": {"sessionId": "claude_old"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(claude_native_hook.time, "time", lambda: 1779922393.245)
    payload = {
        "hook_event_name": "SessionStart",
        "source": "resume",
        "session_id": "claude_fork",
        "session_title": "hello",
        "transcript_path": str(transcript_path),
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(["--bridge-dir", str(bridge_dir)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {
        "systemMessage": "Open this session in Omnigent: http://127.0.0.1:8787/c/conv_fork"
    }
    assert captured.err == ""
    assert requests == [
        ("GET", "http://127.0.0.1:8787/v1/sessions/conv_old", None),
        ("POST", "http://127.0.0.1:8787/v1/sessions/conv_old/fork", {}),
        ("PATCH", "http://127.0.0.1:8787/v1/sessions/conv_fork", {"runner_id": "runner_one"}),
        (
            "POST",
            (
                "http://127.0.0.1:8787/v1/sessions/conv_old/resources/"
                "terminals/terminal_claude_main/transfer"
            ),
            {"target_session_id": "conv_fork"},
        ),
        ("PATCH", "http://127.0.0.1:8787/v1/sessions/conv_old", {"runner_id": ""}),
    ]
    recorded = (bridge_dir / "hooks.jsonl").read_text(encoding="utf-8")
    assert '"omnigent_previous_claude_session_id":"claude_old"' in recorded
    assert '"omnigent_claude_session_was_seen":false' in recorded
    assert '"omnigent_fork_detected":true' in recorded
    assert '"omnigent_fork_rotated_to":"conv_fork"' in recorded
    # The /fork rotation gates Claude's welcome banner and must fail
    # fast — it uses _SESSION_ROTATION_TIMEOUT_S, NOT the day-long
    # permission long-poll budget. If this regresses to
    # _PERMISSION_TIMEOUT_S (86400) an unresponsive Omnigent server would hang
    # the banner for a full day instead of returning None so the
    # background forwarder can fork.
    rotation_timeout = _FakeHttpxClient.captured_timeouts[0]
    assert isinstance(rotation_timeout, httpx.Timeout)
    assert rotation_timeout.read == claude_native_hook._SESSION_ROTATION_TIMEOUT_S


def test_resume_session_start_without_branch_marker_does_not_fork(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Ordinary Claude resumes do not create Omnigent forks.

    This fails if every ``SessionStart source=resume`` starts forking
    Omnigent sessions, which would break normal Claude resume flows.
    """

    class _FailingHttpxClient:
        """
        HTTP client stub that fails if fork detection makes Omnigent calls.

        :param headers: Headers passed to :class:`httpx.Client`.
        :param timeout: Timeout passed to :class:`httpx.Client`.
        """

        def __init__(self, *, headers: dict[str, str], timeout: object) -> None:
            """
            Capture constructor inputs.

            :param headers: HTTP headers for AP.
            :param timeout: HTTP timeout object.
            :returns: None.
            """
            del headers, timeout

        def __enter__(self) -> _FailingHttpxClient:
            """
            Enter the context manager.

            :returns: This fake client.
            """
            raise AssertionError("ordinary resume should not call AP")

        def __exit__(self, *args: object) -> None:
            """
            Exit the context manager.

            :param args: Exception details.
            :returns: None.
            """
            del args

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setattr(claude_native_hook.httpx, "Client", _FailingHttpxClient)
    bridge_dir = prepare_bridge_dir(
        "conv_old",
        bridge_id="bridge_shared",
        workspace=tmp_path,
    )
    build_hook_settings(bridge_dir, ap_server_url="http://127.0.0.1:8787")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude_old",
        },
    )
    payload = {
        "hook_event_name": "SessionStart",
        "source": "resume",
        "session_id": "claude_other",
        "transcript_path": str(tmp_path / "resume.jsonl"),
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(["--bridge-dir", str(bridge_dir)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == {
        "systemMessage": "Open this session in Omnigent: http://127.0.0.1:8787/c/conv_old"
    }
    recorded = (bridge_dir / "hooks.jsonl").read_text(encoding="utf-8")
    assert "omnigent_fork_detected" not in recorded
    assert "omnigent_fork_rotated_to" not in recorded


def test_non_session_start_hook_does_not_emit_conversation_url_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Non-startup hooks remain observational and do not write Claude output.

    This fails if Stop/UserPromptSubmit hooks start producing stdout,
    which Claude could interpret as hook output for events that are only
    supposed to update Omnigent bridge state.
    """
    bridge_dir = tmp_path / "bridge"
    payload = {"hook_event_name": "Stop"}
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(
        [
            "--bridge-dir",
            str(bridge_dir),
            "--conversation-url",
            "http://127.0.0.1:8787/c/conv_abc",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == ""


def test_permission_request_hook_posts_to_active_session_from_bridge_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Permission command hook routes to the current active Omnigent session.

    This fails if the hook bakes in the launch conversation id: after
    Claude ``/clear`` rotates the bridge to a new Omnigent session, approval
    requests would still appear on the old conversation.
    """
    posted: dict[str, object] = {}

    class _FakeHttpxClient:
        """
        Minimal sync HTTP client stub for the permission hook.

        :param headers: Headers passed to :class:`httpx.Client`.
        :param timeout: Timeout passed to :class:`httpx.Client`.
        """

        def __init__(self, *, headers: dict[str, str], timeout: object) -> None:
            """
            Capture constructor inputs.

            :param headers: HTTP headers for AP.
            :param timeout: HTTP timeout object.
            :returns: None.
            """
            posted["headers"] = headers
            posted["timeout"] = timeout

        def __enter__(self) -> _FakeHttpxClient:
            """
            Enter the context manager.

            :returns: This fake client.
            """
            return self

        def __exit__(self, *args: object) -> None:
            """
            Exit the context manager.

            :param args: Exception details.
            :returns: None.
            """
            del args

        def post(self, url: str, *, json: dict[str, object]) -> object:
            """
            Record the outgoing Omnigent request.

            :param url: Target Omnigent URL.
            :param json: Request JSON body.
            :returns: HTTP response object.
            """
            import httpx

            posted["url"] = url
            posted["json"] = json
            return httpx.Response(
                200,
                text='{"hookSpecificOutput":{"hookEventName":"PermissionRequest","permissionDecision":"allow"}}',
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setattr(claude_native_hook.httpx, "Client", _FakeHttpxClient)
    bridge_dir = prepare_bridge_dir(
        "conv_old",
        bridge_id="bridge_shared",
        workspace=tmp_path,
    )
    write_active_session_id(bridge_dir, "conv_new")
    build_hook_settings(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        ap_auth_headers={"Authorization": "Bearer xyz"},
    )
    payload = {"hook_event_name": "PermissionRequest", "tool_name": "Bash"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(
        [
            "permission-request",
            "--bridge-dir",
            str(bridge_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert posted["url"] == ("http://127.0.0.1:8787/v1/sessions/conv_new/hooks/permission-request")
    sent = posted["json"]
    assert isinstance(sent, dict)
    # The hook payload is forwarded verbatim, plus the minted re-attach
    # id the server uses to re-park the prompt across severed polls.
    assert {k: v for k, v in sent.items() if k != "_omnigent_elicitation_id"} == payload
    assert re.fullmatch(r"elicit_claude_[0-9a-f]{32}", sent["_omnigent_elicitation_id"]), (
        f"re-attach id outside the claude-hook namespace: {sent.get('_omnigent_elicitation_id')!r}"
    )
    assert posted["headers"] == {"Authorization": "Bearer xyz"}
    assert json.loads(captured.out)["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert captured.err == ""


def _prepare_permission_bridge(tmp_path: Path, session_id: str) -> Path:
    """
    Stand up a bridge dir wired for the permission-request hook.

    :param tmp_path: Test-scoped temp directory (already patched as the
        trusted bridge parent by the caller).
    :param session_id: Active Omnigent session id, e.g. ``"conv_x"``.
    :returns: The prepared bridge directory.
    """
    bridge_dir = prepare_bridge_dir(
        session_id,
        bridge_id="bridge_retry",
        workspace=tmp_path,
    )
    build_hook_settings(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        ap_auth_headers={"Authorization": "Bearer xyz"},
    )
    return bridge_dir


def test_permission_request_hook_retries_transport_cut_with_same_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    A severed long-poll is re-POSTed with the SAME re-attach id.

    This is the proxy-cut path that used to fail-ask into an invisible
    terminal prompt for headless sub-agents: one transport error ended
    the hook. The retry must reuse the minted
    ``_omnigent_elicitation_id`` — a fresh id per attempt would park a
    NEW elicitation and orphan the card the server kept alive through
    the re-park grace.
    """
    attempts: list[dict[str, object]] = []

    class _FlakyHttpxClient:
        """
        Client stub: first POST raises a transport error, second succeeds.

        :param headers: Headers passed to :class:`httpx.Client`.
        :param timeout: Timeout passed to :class:`httpx.Client`.
        """

        def __init__(self, *, headers: dict[str, str], timeout: object) -> None:
            """
            Accept and discard constructor inputs.

            :param headers: HTTP headers for AP.
            :param timeout: HTTP timeout object.
            :returns: None.
            """
            del headers, timeout

        def __enter__(self) -> _FlakyHttpxClient:
            """
            Enter the context manager.

            :returns: This fake client.
            """
            return self

        def __exit__(self, *args: object) -> None:
            """
            Exit the context manager.

            :param args: Exception details.
            :returns: None.
            """
            del args

        def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
            """
            Fail the first attempt at the transport layer, then succeed.

            :param url: Target Omnigent URL.
            :param json: Request JSON body.
            :returns: HTTP 200 with a decision on the second attempt.
            :raises httpx.ReadError: On the first attempt.
            """
            attempts.append(dict(json))
            if len(attempts) == 1:
                raise httpx.ReadError(
                    "proxy severed the long-poll",
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200,
                text=(
                    '{"hookSpecificOutput":{"hookEventName":"PermissionRequest",'
                    '"decision":{"behavior":"allow"}}}'
                ),
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setattr(claude_native_hook.httpx, "Client", _FlakyHttpxClient)
    # Zero backoff keeps the retry loop instant in tests; production
    # waits between attempts.
    monkeypatch.setattr(claude_native_hook, "_PERMISSION_RETRY_INITIAL_BACKOFF_S", 0.0)
    bridge_dir = _prepare_permission_bridge(tmp_path, "conv_retry")
    payload = {"hook_event_name": "PermissionRequest", "tool_name": "Bash"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(["permission-request", "--bridge-dir", str(bridge_dir)])

    captured = capsys.readouterr()
    assert exit_code == 0
    # 2 = one severed attempt + one successful retry. 1 would mean the
    # transport error fail-asked without retrying (the production bug);
    # 3+ would mean a success was retried.
    assert len(attempts) == 2, f"expected 2 attempts, got {len(attempts)}"
    first_id = attempts[0]["_omnigent_elicitation_id"]
    assert re.fullmatch(r"elicit_claude_[0-9a-f]{32}", str(first_id))
    # Same id on the retry is the whole re-attach contract — a new id
    # would orphan the elicitation the server kept pending.
    assert attempts[1]["_omnigent_elicitation_id"] == first_id
    # The verdict from the successful retry reaches Claude on stdout.
    decision = json.loads(captured.out)["hookSpecificOutput"]["decision"]
    assert decision == {"behavior": "allow"}


def test_permission_request_hook_does_not_retry_rejections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    A 4xx from the Omnigent server is a deliberate answer — no retry.

    Retrying a rejection (bad payload, foreign elicitation id) would
    hammer the server with a request it already refused; the hook must
    fail-ask immediately instead.
    """
    attempts: list[str] = []

    class _RejectingHttpxClient:
        """
        Client stub that always returns HTTP 400.

        :param headers: Headers passed to :class:`httpx.Client`.
        :param timeout: Timeout passed to :class:`httpx.Client`.
        """

        def __init__(self, *, headers: dict[str, str], timeout: object) -> None:
            """
            Accept and discard constructor inputs.

            :param headers: HTTP headers for AP.
            :param timeout: HTTP timeout object.
            :returns: None.
            """
            del headers, timeout

        def __enter__(self) -> _RejectingHttpxClient:
            """
            Enter the context manager.

            :returns: This fake client.
            """
            return self

        def __exit__(self, *args: object) -> None:
            """
            Exit the context manager.

            :param args: Exception details.
            :returns: None.
            """
            del args

        def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
            """
            Reject every attempt with HTTP 400.

            :param url: Target Omnigent URL.
            :param json: Request JSON body.
            :returns: HTTP 400 response.
            """
            del json
            attempts.append(url)
            return httpx.Response(
                400,
                text='{"error": "bad payload"}',
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setattr(claude_native_hook.httpx, "Client", _RejectingHttpxClient)
    monkeypatch.setattr(claude_native_hook, "_PERMISSION_RETRY_INITIAL_BACKOFF_S", 0.0)
    bridge_dir = _prepare_permission_bridge(tmp_path, "conv_reject")
    payload = {"hook_event_name": "PermissionRequest", "tool_name": "Bash"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(["permission-request", "--bridge-dir", str(bridge_dir)])

    captured = capsys.readouterr()
    # Fail-ask contract: exit 0 with no stdout so Claude falls back to
    # its TUI prompt.
    assert exit_code == 0
    assert captured.out == ""
    # 1 = the 4xx was treated as final. 2+ means rejections are being
    # retried, hammering the server with refused requests.
    assert len(attempts) == 1, f"expected a single attempt, got {len(attempts)}"
    assert "rejected" in captured.err


def test_build_hook_settings_registers_policy_hooks_when_omnigent_server_url_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``build_hook_settings`` includes PreToolUse and PostToolUse policy hooks.

    Without these entries, Claude Code's native tools (Bash, Edit, Write,
    etc.) bypass policy evaluation entirely — only relay/MCP tools would
    be gated. This fails if the hook registration is dropped or guarded
    behind a different condition than ``ap_server_url``.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = prepare_bridge_dir(
        "conv_abc",
        bridge_id="bridge_test",
        workspace=tmp_path,
    )
    settings = build_hook_settings(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
    )
    hooks = settings["hooks"]
    # PreToolUse and PostToolUse must be registered alongside PermissionRequest.
    assert "PreToolUse" in hooks, (
        "PreToolUse hook not registered — native tools bypass TOOL_CALL policy evaluation"
    )
    assert "PermissionRequest" in hooks
    # PreToolUse has two entries: the AskUserQuestion-specific hook first,
    # then the catch-all policy evaluation hook.
    assert len(hooks["PreToolUse"]) == 2, (
        f"Expected 2 PreToolUse entries (AskUserQuestion + catch-all policy), "
        f"got {len(hooks['PreToolUse'])}"
    )
    # First entry: AskUserQuestion-specific hook with matcher.
    ask_uq_entry = hooks["PreToolUse"][0]
    assert ask_uq_entry.get("matcher") == "AskUserQuestion"
    ask_uq_cmd = ask_uq_entry["hooks"][0]["command"]
    assert "ask-user-question" in ask_uq_cmd
    assert str(bridge_dir) in ask_uq_cmd
    # Second entry: catch-all policy evaluation hook (no matcher).
    policy_entry = hooks["PreToolUse"][1]
    assert "matcher" not in policy_entry
    pre_tool_use_cmd = policy_entry["hooks"][0]["command"]
    assert "evaluate-policy" in pre_tool_use_cmd
    assert str(bridge_dir) in pre_tool_use_cmd
    # PostToolUse has observer hooks (TodoWrite, TaskUpdate) PLUS the policy
    # evaluation hook appended as a catch-all entry.
    post_tool_use_entries = hooks["PostToolUse"]
    # At least 3 entries: TodoWrite matcher, TaskUpdate matcher, catch-all policy.
    assert len(post_tool_use_entries) >= 3, (
        f"Expected >= 3 PostToolUse entries (TodoWrite + TaskUpdate + policy), "
        f"got {len(post_tool_use_entries)}"
    )
    # The last entry is the catch-all policy evaluation hook.
    policy_entry_cmd = post_tool_use_entries[-1]["hooks"][0]["command"]
    assert "evaluate-policy" in policy_entry_cmd
    # UserPromptSubmit carries the forwarder's status hook PLUS the policy
    # hook appended as a catch-all. For native sessions this is the sole
    # REQUEST-phase gate (the server-level _evaluate_input_policy skips
    # native message events), so a missing policy hook here means native
    # prompts reach the model with no request-phase policy.
    user_prompt_entries = hooks["UserPromptSubmit"]
    user_prompt_cmds = [h["command"] for entry in user_prompt_entries for h in entry["hooks"]]
    assert any("evaluate-policy" in cmd for cmd in user_prompt_cmds), (
        f"UserPromptSubmit policy hook not registered; got {user_prompt_cmds!r}"
    )
    # The forwarder's status hook must survive (the policy hook is appended).
    assert any("evaluate-policy" not in cmd for cmd in user_prompt_cmds)


def test_build_hook_settings_registers_message_display_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``build_hook_settings`` wires ``MessageDisplay`` to the fast appender.

    Without this entry, Claude never invokes the deltas-appender and live
    token streaming silently does nothing (the web UI falls back to the
    whole-message-on-completion behavior). It must route to the dedicated
    stdlib-only module — NOT the heavier observer hook — so the per-chunk
    hot path stays cheap, and it must NOT depend on ``ap_server_url``
    (streaming works for local servers too). Fails if the registration
    is dropped or pointed at the wrong module.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = prepare_bridge_dir("conv_abc", bridge_id="bridge_test", workspace=tmp_path)

    # No ap_server_url: streaming must still be registered.
    settings = build_hook_settings(bridge_dir)
    hooks = settings["hooks"]
    assert "MessageDisplay" in hooks, (
        "MessageDisplay hook not registered — live token streaming is dead"
    )
    command = hooks["MessageDisplay"][0]["hooks"][0]["command"]
    # Routes to the dedicated lightweight module with this bridge dir...
    assert "omnigent.claude_native_message_display_hook" in command
    assert str(bridge_dir) in command
    # ...and NOT through the heavier observer/policy subcommands (which
    # would import claude_native_bridge on every streamed chunk).
    assert "evaluate-policy" not in command
    assert "permission-request" not in command


def test_build_hook_settings_omits_policy_hooks_without_omnigent_server_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``build_hook_settings`` omits policy hooks when no Omnigent URL is set.

    Without an Omnigent server there are no policies to evaluate; registering
    the hooks would cause no-op subprocesses on every tool call.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = prepare_bridge_dir(
        "conv_abc",
        bridge_id="bridge_test",
        workspace=tmp_path,
    )
    settings = build_hook_settings(bridge_dir)
    hooks = settings["hooks"]
    assert "PreToolUse" not in hooks
    assert "PermissionRequest" not in hooks
    # PostToolUse still has the observer hooks (TodoWrite, TaskUpdate)
    # but NOT the policy evaluation hook.
    for entry in hooks.get("PostToolUse", []):
        cmd = entry["hooks"][0]["command"]
        assert "evaluate-policy" not in cmd, (
            "Policy evaluation hook should not be registered without Omnigent URL"
        )


def test_evaluate_policy_pre_tool_use_converts_and_returns_deny(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    PreToolUse payload is converted to proto schema and deny verdict is returned.

    This fails if the subprocess doesn't convert the Claude payload to
    the EvaluationRequest proto format, doesn't POST to the correct
    ``/policies/evaluate`` endpoint, or doesn't convert the
    EvaluationResponse back to Claude's PreToolUse hook output.
    """
    posted: dict[str, object] = {}

    class _FakeHttpxClient:
        """
        Minimal sync HTTP client stub for the evaluate-policy hook.

        :param headers: Headers passed to :class:`httpx.Client`.
        :param timeout: Timeout passed to :class:`httpx.Client`.
        """

        def __init__(self, *, headers: dict[str, str], timeout: object) -> None:
            """
            Capture constructor inputs.

            :param headers: HTTP headers for AP.
            :param timeout: HTTP timeout object.
            :returns: None.
            """
            posted["headers"] = headers
            posted["timeout"] = timeout

        def __enter__(self) -> _FakeHttpxClient:
            """
            Enter the context manager.

            :returns: This fake client.
            """
            return self

        def __exit__(self, *args: object) -> None:
            """
            Exit the context manager.

            :param args: Exception details.
            :returns: None.
            """
            del args

        def post(self, url: str, *, json: dict[str, object]) -> object:
            """
            Record the outgoing Omnigent request and return a DENY verdict.

            :param url: Target Omnigent URL.
            :param json: Request JSON body (EvaluationRequest).
            :returns: HTTP response object with EvaluationResponse.
            """
            import httpx

            posted["url"] = url
            posted["json"] = json
            return httpx.Response(
                200,
                text=('{"result":"POLICY_ACTION_DENY","reason":"Blocked by policy"}'),
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setattr(claude_native_hook.httpx, "Client", _FakeHttpxClient)
    bridge_dir = prepare_bridge_dir(
        "conv_abc",
        bridge_id="bridge_shared",
        workspace=tmp_path,
    )
    write_active_session_id(bridge_dir, "conv_active")
    build_hook_settings(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        ap_auth_headers={"Authorization": "Bearer test-token"},
    )
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /"},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(
        [
            "evaluate-policy",
            "--bridge-dir",
            str(bridge_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    # Verify the hook posted to the /policies/evaluate endpoint.
    assert posted["url"] == ("http://127.0.0.1:8787/v1/sessions/conv_active/policies/evaluate")
    # The payload is converted to proto EvaluationRequest format.
    sent = posted["json"]
    assert sent["event"]["type"] == "PHASE_TOOL_CALL"
    assert sent["event"]["data"]["name"] == "Bash"
    assert sent["event"]["data"]["arguments"] == {"command": "rm -rf /"}
    # Auth headers from permission_hook.json are sent.
    assert posted["headers"] == {"Authorization": "Bearer test-token"}
    # The EvaluationResponse is converted back to Claude's PreToolUse format.
    result = json.loads(captured.out)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result["hookSpecificOutput"]["permissionDecisionReason"] == "Blocked by policy"
    assert captured.err == ""


def test_evaluate_policy_stamps_live_model_from_context_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The hook stamps the statusLine-captured live model into the request.

    The statusLine wrapper writes the active model id into ``context.json``
    on every render. The hook must stamp it (and ``harness``) onto the
    evaluation request so the cost-budget gate sees the CURRENT model at gate
    time — not the lagging ``model_override`` mirror. Regression guard for a
    cheap-model session getting blocked over budget because the model was
    unresolved (None) and the gate failed closed.
    """
    posted: dict[str, object] = {}

    class _FakeHttpxClient:
        """Sync HTTP client stub capturing the posted EvaluationRequest."""

        def __init__(self, *, headers: dict[str, str], timeout: object) -> None:
            """Record constructor inputs. :returns: None."""
            del headers, timeout

        def __enter__(self) -> _FakeHttpxClient:
            """:returns: This fake client."""
            return self

        def __exit__(self, *args: object) -> None:
            """:returns: None."""
            del args

        def post(self, url: str, *, json: dict[str, object]) -> object:
            """Record the request and return an ALLOW verdict. :returns: response."""
            import httpx

            posted["json"] = json
            return httpx.Response(
                200,
                text='{"result":"POLICY_ACTION_ALLOW"}',
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setattr(claude_native_hook.httpx, "Client", _FakeHttpxClient)
    bridge_dir = prepare_bridge_dir("conv_abc", bridge_id="bridge_shared", workspace=tmp_path)
    write_active_session_id(bridge_dir, "conv_active")
    build_hook_settings(bridge_dir, ap_server_url="http://127.0.0.1:8787")
    # The statusLine wrapper's capture: the live model the user is on.
    (bridge_dir / "context.json").write_text(
        json.dumps({"model": "claude-sonnet-4-6"}), encoding="utf-8"
    )
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(["evaluate-policy", "--bridge-dir", str(bridge_dir)])

    assert exit_code == 0
    context = posted["json"]["event"]["context"]
    # The live model + harness must ride the request so the cost gate doesn't
    # fail closed on an unresolved model.
    assert context["model"] == "claude-sonnet-4-6"
    assert context["harness"] == "claude-native"


def test_evaluate_policy_post_tool_use_converts_and_returns_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    PostToolUse payload is converted to PHASE_TOOL_RESULT and deny surfaces as context.

    PostToolUse hooks are observational — they can't block the tool call.
    A DENY verdict is surfaced as ``additionalContext`` so Claude sees
    the policy warning alongside the tool result.
    """
    posted: dict[str, object] = {}

    class _FakeHttpxClient:
        """
        Minimal sync HTTP client stub for PostToolUse evaluation.

        :param headers: Headers passed to :class:`httpx.Client`.
        :param timeout: Timeout passed to :class:`httpx.Client`.
        """

        def __init__(self, *, headers: dict[str, str], timeout: object) -> None:
            """
            Capture constructor inputs.

            :param headers: HTTP headers for AP.
            :param timeout: HTTP timeout object.
            :returns: None.
            """
            posted["headers"] = headers

        def __enter__(self) -> _FakeHttpxClient:
            """
            Enter the context manager.

            :returns: This fake client.
            """
            return self

        def __exit__(self, *args: object) -> None:
            """
            Exit the context manager.

            :param args: Exception details.
            :returns: None.
            """
            del args

        def post(self, url: str, *, json: dict[str, object]) -> object:
            """
            Record the outgoing Omnigent request and return a DENY verdict.

            :param url: Target Omnigent URL.
            :param json: Request JSON body (EvaluationRequest).
            :returns: HTTP response with EvaluationResponse.
            """
            import httpx

            posted["url"] = url
            posted["json"] = json
            return httpx.Response(
                200,
                text='{"result":"POLICY_ACTION_DENY","reason":"Sensitive data in output"}',
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setattr(claude_native_hook.httpx, "Client", _FakeHttpxClient)
    bridge_dir = prepare_bridge_dir(
        "conv_abc",
        bridge_id="bridge_shared",
        workspace=tmp_path,
    )
    write_active_session_id(bridge_dir, "conv_active")
    build_hook_settings(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
    )
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "cat /etc/passwd"},
        "tool_output": "root:x:0:0:root:/root:/bin/bash",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(
        [
            "evaluate-policy",
            "--bridge-dir",
            str(bridge_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    # Verify the proto request is PHASE_TOOL_RESULT with request_data.
    sent = posted["json"]
    assert sent["event"]["type"] == "PHASE_TOOL_RESULT"
    assert sent["event"]["data"]["result"] == "root:x:0:0:root:/root:/bin/bash"
    assert sent["event"]["request_data"]["name"] == "Bash"
    assert sent["event"]["request_data"]["arguments"] == {"command": "cat /etc/passwd"}
    # PostToolUse DENY surfaces as additionalContext warning.
    result = json.loads(captured.out)
    assert "Policy violation" in result["hookSpecificOutput"]["additionalContext"]
    assert "Sensitive data in output" in result["hookSpecificOutput"]["additionalContext"]
    assert captured.err == ""


def test_ask_user_question_hook_noop_in_non_bypass_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    ``ask-user-question`` subcommand is a no-op when not in bypassPermissions mode.

    In default / acceptEdits / plan modes the ``PermissionRequest`` hook fires
    and owns the elicitation.  The ``ask-user-question`` PreToolUse hook must
    return empty output (no opinion) so the form is not shown twice.

    This fails if the handler forwards the payload to Omnigent in non-bypass mode —
    which would cause a duplicate elicitation card in the web UI and race for
    the same answer.
    """
    calls: list[str] = []

    class _RaisesIfCalled:
        """HTTP client stub that fails the test if called unexpectedly."""

        def __init__(self, **_kwargs: object) -> None:
            """
            Record unexpected construction.

            :param _kwargs: Ignored constructor args.
            :returns: None.
            """
            calls.append("constructed")

        def __enter__(self) -> _RaisesIfCalled:
            """
            Enter context — should not be reached.

            :returns: self.
            """
            return self

        def __exit__(self, *_args: object) -> None:
            """
            Exit context — should not be reached.

            :param _args: Ignored exception args.
            :returns: None.
            """

        def post(self, *_args: object, **_kwargs: object) -> object:
            """
            Fail if Omnigent is called — must not happen in non-bypass mode.

            :param _args: Ignored.
            :param _kwargs: Ignored.
            :returns: Never.
            :raises AssertionError: Always, so the test fails visibly.
            """
            raise AssertionError(
                "AP was called for ask-user-question in non-bypass mode — "
                "PermissionRequest hook should own the elicitation instead"
            )

    monkeypatch.setattr(claude_native_hook.httpx, "Client", _RaisesIfCalled)
    bridge_dir = prepare_bridge_dir("conv_abc", bridge_id="b1", workspace=tmp_path)
    write_active_session_id(bridge_dir, "conv_abc")
    build_hook_settings(bridge_dir, ap_server_url="http://127.0.0.1:8787")

    for mode in ("default", "acceptEdits", "plan", None):
        payload: dict[str, object] = {
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": []},
        }
        if mode is not None:
            payload["permission_mode"] = mode
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        exit_code = claude_native_hook.main(["ask-user-question", "--bridge-dir", str(bridge_dir)])
        captured = capsys.readouterr()
        # No Omnigent call, no output — "no opinion" so PermissionRequest takes over.
        assert exit_code == 0, f"Non-zero exit for mode={mode!r}"
        assert captured.out == "", f"Unexpected output for mode={mode!r}: {captured.out!r}"
        assert calls == [], f"AP client was constructed for mode={mode!r}"


def test_ask_user_question_hook_posts_and_returns_pre_tool_use_output_in_bypass_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    In bypassPermissions mode the hook posts to Omnigent and returns PreToolUse output.

    In bypass mode ``PermissionRequest`` never fires, so this PreToolUse hook
    is the only opportunity to surface ``AskUserQuestion`` in the web UI.  It
    must POST the payload to the Omnigent session's permission-request endpoint, then
    convert the ``PermissionRequest``-format response to ``PreToolUse`` format
    (lifting ``decision.updatedInput`` to the top-level ``updatedInput`` field).

    Fails if: Omnigent is not called in bypass mode, the URL targets the wrong session,
    the response is not converted from PermissionRequest to PreToolUse format,
    or the user's answers are not surfaced in ``updatedInput``.
    """
    posted: dict[str, object] = {}
    answers = {"q1": "Option A"}
    server_response = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "allow",
                "updatedInput": {
                    "questions": [{"question": "Pick one", "options": [{"label": "Option A"}]}],
                    "answers": answers,
                },
            },
        }
    }

    class _FakeHttpxClient:
        """
        Minimal sync HTTP client stub for the ask-user-question hook.

        :param headers: Headers passed to :class:`httpx.Client`.
        :param timeout: Timeout passed to :class:`httpx.Client`.
        """

        def __init__(self, *, headers: dict[str, str], timeout: object) -> None:
            """
            Capture constructor inputs.

            :param headers: HTTP headers.
            :param timeout: Request timeout.
            :returns: None.
            """
            posted["headers"] = headers
            posted["timeout"] = timeout

        def __enter__(self) -> _FakeHttpxClient:
            """
            Enter context manager.

            :returns: self.
            """
            return self

        def __exit__(self, *_args: object) -> None:
            """
            Exit context manager.

            :param _args: Ignored.
            :returns: None.
            """

        def post(self, url: str, *, json: dict[str, object]) -> object:
            """
            Record the Omnigent request and return a canned PermissionRequest response.

            :param url: Target URL.
            :param json: Request body.
            :returns: Fake HTTP response.
            """
            import httpx as _httpx

            posted["url"] = url
            posted["json"] = json
            import json as _json

            return _httpx.Response(
                200,
                text=_json.dumps(server_response),
                request=_httpx.Request("POST", url),
            )

    monkeypatch.setattr(claude_native_hook.httpx, "Client", _FakeHttpxClient)
    bridge_dir = prepare_bridge_dir("conv_bypass", bridge_id="b2", workspace=tmp_path)
    write_active_session_id(bridge_dir, "conv_bypass")
    build_hook_settings(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        ap_auth_headers={"Authorization": "Bearer token"},
    )
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [{"question": "Pick one", "options": [{"label": "Option A"}]}]
        },
        "permission_mode": "bypassPermissions",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(["ask-user-question", "--bridge-dir", str(bridge_dir)])

    captured = capsys.readouterr()
    assert exit_code == 0
    # Omnigent must be called with the active session's URL.
    assert posted["url"] == (
        "http://127.0.0.1:8787/v1/sessions/conv_bypass/hooks/permission-request"
    )
    # The full PreToolUse payload (including permission_mode) is
    # forwarded verbatim, plus the minted re-attach id.
    sent = posted["json"]
    assert isinstance(sent, dict)
    assert {k: v for k, v in sent.items() if k != "_omnigent_elicitation_id"} == payload
    assert re.fullmatch(r"elicit_claude_[0-9a-f]{32}", sent["_omnigent_elicitation_id"])
    # Auth headers from bridge config are forwarded.
    assert posted["headers"] == {"Authorization": "Bearer token"}
    # Output must be PreToolUse-format, NOT PermissionRequest-format.
    result = json.loads(captured.out)
    hs = result["hookSpecificOutput"]
    assert hs["hookEventName"] == "PreToolUse", (
        "Response was not converted from PermissionRequest to PreToolUse format"
    )
    assert hs["permissionDecision"] == "allow"
    # User answers must be lifted into top-level updatedInput so Claude skips
    # its TUI picker and uses the web form's selections.
    assert hs["updatedInput"]["answers"] == answers, (
        "User answers were not propagated in updatedInput — Claude will fall back "
        "to its TUI picker and ignore the web form selection"
    )
    assert captured.err == ""


def test_ask_user_question_hook_returns_deny_without_updated_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    When the user denies AskUserQuestion in bypass mode, hook output is deny with no updatedInput.

    A denial blocks the tool call entirely.  There are no answers to inject, so
    ``updatedInput`` must be absent from the PreToolUse output.

    Fails if ``updatedInput`` is included on a deny (which would produce a
    malformed output and confuse Claude), or if the denial is not surfaced.
    """
    server_response = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "deny"},
        }
    }

    class _FakeHttpxClient:
        """Fake HTTP client returning a deny response."""

        def __init__(self, **_kwargs: object) -> None:
            """
            Accept constructor kwargs.

            :param _kwargs: Ignored.
            :returns: None.
            """

        def __enter__(self) -> _FakeHttpxClient:
            """
            Enter context.

            :returns: self.
            """
            return self

        def __exit__(self, *_args: object) -> None:
            """
            Exit context.

            :param _args: Ignored.
            :returns: None.
            """

        def post(self, url: str, *, json: object) -> object:
            """
            Return the canned deny response.

            :param url: Ignored.
            :param json: Ignored.
            :returns: Fake HTTP response.
            """
            import json as _json

            import httpx as _httpx

            return _httpx.Response(
                200,
                text=_json.dumps(server_response),
                request=_httpx.Request("POST", url),
            )

    monkeypatch.setattr(claude_native_hook.httpx, "Client", _FakeHttpxClient)
    bridge_dir = prepare_bridge_dir("conv_deny", bridge_id="b3", workspace=tmp_path)
    write_active_session_id(bridge_dir, "conv_deny")
    build_hook_settings(bridge_dir, ap_server_url="http://127.0.0.1:8787")
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": []},
        "permission_mode": "bypassPermissions",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(["ask-user-question", "--bridge-dir", str(bridge_dir)])

    captured = capsys.readouterr()
    assert exit_code == 0
    result = json.loads(captured.out)
    hs = result["hookSpecificOutput"]
    assert hs["hookEventName"] == "PreToolUse"
    assert hs["permissionDecision"] == "deny"
    # No updatedInput on deny — answers are meaningless when the tool is blocked.
    assert "updatedInput" not in hs, (
        "updatedInput must not appear on a deny response — there are no answers to inject"
    )


@pytest.mark.parametrize("mode", ["connect_error", "non_2xx", "empty_body", "malformed_json"])
def test_evaluate_policy_pre_tool_use_fails_closed_when_verdict_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    """
    A governed PreToolUse call denies when no usable verdict is returned.

    For native harnesses this hook is the sole TOOL_CALL enforcement point,
    so a server outage / non-2xx / empty / malformed response must fail
    CLOSED (deny) instead of "no opinion" — the bypass reported in #536.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setattr(claude_native_hook.httpx, "Client", make_failing_client(mode))
    bridge_dir = prepare_bridge_dir("conv_abc", bridge_id="bridge_shared", workspace=tmp_path)
    write_active_session_id(bridge_dir, "conv_active")
    build_hook_settings(bridge_dir, ap_server_url="http://127.0.0.1:8787")
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /"},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(["evaluate-policy", "--bridge-dir", str(bridge_dir)])

    captured = capsys.readouterr()
    assert exit_code == 0
    result = json.loads(captured.out)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny", result
    assert result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_output": "ok",
        },
        {"hook_event_name": "UserPromptSubmit", "prompt": "hello"},
    ],
)
def test_evaluate_policy_non_tool_call_phases_fail_open_on_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    """
    Off the tool-call gate, an unobtainable verdict stays fail-open.

    PostToolUse runs after the tool executed and the request gate is
    advisory, so neither denies on a transport error — mirroring the
    runner-side ``FAIL_CLOSED_PHASES`` (PR #163).
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setattr(claude_native_hook.httpx, "Client", make_failing_client("connect_error"))
    bridge_dir = prepare_bridge_dir("conv_abc", bridge_id="bridge_shared", workspace=tmp_path)
    write_active_session_id(bridge_dir, "conv_active")
    build_hook_settings(bridge_dir, ap_server_url="http://127.0.0.1:8787")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = claude_native_hook.main(["evaluate-policy", "--bridge-dir", str(bridge_dir)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
