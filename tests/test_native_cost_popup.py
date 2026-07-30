"""
Unit tests for the native-terminal cost-approval popup script.

Covers :mod:`omnigent.native_cost_popup` — the program that runs inside
a ``tmux display-popup`` on a native harness pane, reads an
approve/decline answer, and POSTs the verdict to the Omnigent elicitation-
resolve endpoint (the same endpoint the web ApprovalCard uses).

The tests drive the public :func:`omnigent.native_cost_popup.main`
entry point and assert on the exact HTTP request it issues (URL, method,
JSON body) and on the no-request invariant when the popup is dismissed.
"""

from __future__ import annotations

import json
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import request

import pytest

from omnigent import native_cost_popup

_AP_URL = "http://127.0.0.1:8787"
_SESSION_ID = "conv_abc123"
_ELICITATION_ID = "elicit_deadbeef"
_MESSAGE = "Session cost $0.12 crossed the $0.10 checkpoint. Continue?"


@dataclass
class _CapturedRequest:
    """
    The single HTTP request the popup script issued during a test.

    :param url: Full request URL, e.g.
        ``"http://127.0.0.1:8787/v1/sessions/conv_abc123/elicitations/elicit_x/resolve"``.
    :param method: HTTP verb, e.g. ``"POST"``.
    :param body: Decoded JSON request body, e.g. ``{"action": "accept"}``.
    :param headers: Request headers as sent, e.g.
        ``{"Content-type": "application/json", "Authorization": "Bearer t"}``.
    """

    url: str
    method: str
    body: dict[str, Any]
    headers: dict[str, str]


@dataclass
class _PopupHarness:
    """
    Wiring that lets a test drive ``main`` without a real terminal/AP.

    :param config_file: Path to the AP-routing config the script reads.
    :param captured: The request the script POSTed, or ``None`` if it
        issued no request (the dismissal path).
    """

    config_file: Path
    captured: _CapturedRequest | None = field(default=None)


class _FakeResponse:
    """Minimal context-manager stand-in for an ``http.client`` response.

    :param status: HTTP status code the fake returns, e.g. ``202``.
    """

    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _install_popup_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    answer: str | None,
    auth_headers: dict[str, str] | None = None,
) -> _PopupHarness:
    """
    Wire ``input`` and ``urlopen`` so ``main`` runs offline and is observable.

    Writes a real AP-routing config file (so ``_read_omnigent_routing`` exercises
    its real parse), stubs ``input`` to return *answer* (or raise EOF for a
    dismissal), and replaces the module's ``request`` binding with a
    namespace whose ``urlopen`` records the outgoing request — scoped to
    this module only, never the global ``urllib.request`` (rule 14).

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir for the config file.
    :param answer: Text the stubbed ``input`` returns, e.g. ``"y"``;
        ``None`` makes ``input`` raise ``EOFError`` (dismissal).
    :param auth_headers: ``ap_auth_headers`` to write into the config,
        e.g. ``{"Authorization": "Bearer t"}``. Defaults to one header.
    :returns: A :class:`_PopupHarness` exposing the config path and the
        captured request (populated when ``main`` POSTs).
    """
    headers = {"Authorization": "Bearer test-token"} if auth_headers is None else auth_headers
    config_file = tmp_path / "permission_hook.json"
    config_file.write_text(
        json.dumps({"ap_server_url": _AP_URL, "ap_auth_headers": headers}),
        encoding="utf-8",
    )
    harness = _PopupHarness(config_file=config_file)

    def _fake_input(_prompt: str = "") -> str:
        if answer is None:
            raise EOFError
        return answer

    monkeypatch.setattr("builtins.input", _fake_input)

    def _fake_urlopen(req: request.Request, timeout: float | None = None) -> _FakeResponse:
        harness.captured = _CapturedRequest(
            url=req.full_url,
            method=req.get_method(),
            body=json.loads(req.data.decode("utf-8")),
            headers=dict(req.header_items()),
        )
        return _FakeResponse(202)

    # Replace only this module's ``request`` name (keep the real
    # ``Request`` builder; fake only ``urlopen``) — never the global
    # urllib.request singleton.
    monkeypatch.setattr(
        native_cost_popup,
        "request",
        types.SimpleNamespace(Request=request.Request, urlopen=_fake_urlopen),
    )
    # Disable the resolved-elsewhere watcher: it spawns a daemon thread that
    # polls the server and ``os._exit``s, neither of which belongs in a unit
    # test of the verdict→resolve path (the watcher is manual-E2E verified).
    monkeypatch.setattr(native_cost_popup, "_start_resolution_watcher", lambda **_kw: None)
    return harness


def _run_main(harness: _PopupHarness) -> int:
    """
    Invoke ``main`` with the standard argv against *harness*'s config.

    :param harness: The wired harness from :func:`_install_popup_harness`.
    :returns: ``main``'s integer exit code.
    """
    return native_cost_popup.main(
        [
            "--config-file",
            str(harness.config_file),
            "--session-id",
            _SESSION_ID,
            "--elicitation-id",
            _ELICITATION_ID,
            "--message",
            _MESSAGE,
        ]
    )


@pytest.mark.parametrize(
    "answer,expected_action",
    [
        ("y", "accept"),
        ("yes", "accept"),
        ("n", "decline"),
        ("no", "decline"),
    ],
)
def test_main_posts_verdict_to_resolve_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    answer: str,
    expected_action: str,
) -> None:
    """
    A y/n answer POSTs the matching verdict to the resolve URL.

    This is the popup's whole job: map the terminal answer to an MCP
    ``ElicitationResult`` and deliver it to the SAME endpoint the web
    ApprovalCard hits, so the shared elicitation Future resolves. A
    failure means the verdict mapping, the URL path, or the body shape
    regressed — any of which would silently break native approval.
    """
    harness = _install_popup_harness(monkeypatch, tmp_path, answer=answer)

    rc = _run_main(harness)

    assert rc == 0  # clean exit after a delivered verdict
    assert harness.captured is not None, (
        "main answered y/n but issued no HTTP request — the resolve POST "
        "was skipped, so the native verdict never reaches the server."
    )
    # URL must be the exact session-scoped resolve endpoint the web card
    # uses; a wrong prefix/path would 404 and the verdict would be lost.
    assert harness.captured.url == (
        f"{_AP_URL}/v1/sessions/{_SESSION_ID}/elicitations/{_ELICITATION_ID}/resolve"
    )
    assert harness.captured.method == "POST"
    # Body is the MCP ElicitationResult action; if this maps wrong, an
    # approve could decline (stop) the session or vice versa.
    assert harness.captured.body == {"action": expected_action}
    # Auth header from the config file must be forwarded so the
    # owner-gated resolve endpoint accepts the call.
    assert harness.captured.headers.get("Authorization") == "Bearer test-token"


def test_main_dismissal_issues_no_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Dismissing the popup (EOF/Ctrl-C) resolves nothing.

    Leaving the elicitation outstanding is the contract: the user can
    still answer in the web UI, or the server-side timeout decides. If
    dismissal synthesized a verdict, a stray keystroke could stop a
    session (or silently approve continued spend).
    """
    harness = _install_popup_harness(monkeypatch, tmp_path, answer=None)

    rc = _run_main(harness)

    assert rc == 0  # dismissal is a clean, non-error exit
    assert harness.captured is None, (
        "main posted a verdict on dismissal — it must leave the "
        "elicitation outstanding for the web UI / server timeout."
    )


def test_prompt_header_reflects_policy_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    The modal header names the deciding policy, not a fixed cost label.

    The popup serves *any* tool-policy ASK, so a hardcoded "Cost budget
    checkpoint" header mislabels every non-cost policy (the reported
    bug). Passing ``policy_name`` must surface that name in the header,
    and the stale cost-specific label must be gone.
    """
    # Answer immediately so the prompt loop exits after one read; we only
    # care about what was printed before the input.
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")

    action = native_cost_popup._prompt_verdict(_MESSAGE, policy_name="block-rm-rf")

    assert action == "accept"
    out = capsys.readouterr().out
    assert "block-rm-rf" in out, (
        "the deciding policy name must appear in the popup header so the "
        "user knows which policy is asking"
    )
    assert "Cost budget checkpoint" not in out, (
        "the hardcoded cost-budget header must not render for a non-cost policy — that was the bug"
    )


def test_prompt_header_falls_back_when_no_policy_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    A missing policy name yields a generic header, never the cost label.

    When the deciding policy is unknown the header must degrade to a
    neutral "Approval required" rather than the old cost-specific
    string, so no surface ever mislabels the prompt.
    """
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")

    action = native_cost_popup._prompt_verdict(_MESSAGE, policy_name=None)

    assert action == "decline"
    out = capsys.readouterr().out
    assert "Approval required" in out
    assert "Cost budget checkpoint" not in out


def test_main_missing_omnigent_server_url_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A config file without ``ap_server_url`` aborts via ``SystemExit``.

    There is no safe default for "where is the Omnigent server", so the script
    fails loud (the popup just closes; the web card remains answerable)
    rather than POSTing to a guessed URL.
    """
    config_file = tmp_path / "permission_hook.json"
    config_file.write_text(json.dumps({"ap_auth_headers": {}}), encoding="utf-8")
    # Answer 'y' so the script gets past the prompt and reaches the
    # config read (the failure point under test).
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")

    with pytest.raises(SystemExit):
        native_cost_popup.main(
            [
                "--config-file",
                str(config_file),
                "--session-id",
                _SESSION_ID,
                "--elicitation-id",
                _ELICITATION_ID,
                "--message",
                _MESSAGE,
            ]
        )


def test_wait_for_tmux_client_returns_true_when_client_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Returns ``True`` immediately when a client is already attached.

    The terminal-attach re-pop path waits on this before re-popping a
    pending cost approval; if it never sees the attaching client the popup
    would be skipped. Returning True without polling proves the happy path
    doesn't add latency.
    """
    monkeypatch.setattr(native_cost_popup, "_list_tmux_clients", lambda _s, _t: ["/dev/pts/9"])

    # Client present on the first probe → True with no sleep.
    assert native_cost_popup.wait_for_tmux_client("/tmp/x.sock", "main", timeout_s=5.0) is True


def test_wait_for_tmux_client_times_out_when_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Returns ``False`` when no client attaches within the window.

    A zero deadline exits before the first sleep, so the caller (the
    re-pop task) gives up and leaves the web card as the surface rather
    than firing a popup at a pane with nothing to render it.
    """
    monkeypatch.setattr(native_cost_popup, "_list_tmux_clients", lambda _s, _t: [])

    # timeout_s=0.0 → the deadline has already passed, so it returns without
    # sleeping (keeps the test fast and free of time.sleep).
    assert native_cost_popup.wait_for_tmux_client("/tmp/x.sock", "main", timeout_s=0.0) is False


def test_main_notice_mode_needs_no_config_and_no_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--notice`` shows a hard-block reason and exits 0 without any server call.

    The hard-DENY path (e.g. an opencode cost cap) has nothing to resolve — it
    must not require the AP-routing config and must not POST a verdict.
    """
    posted: list[Any] = []
    monkeypatch.setattr(request, "urlopen", lambda *a, **k: posted.append(a))  # type: ignore[arg-type]
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    rc = native_cost_popup.main(
        ["--notice", "--message", "You've hit the $0.0001 budget.", "--policy-name", "cost-budget"]
    )
    assert rc == 0
    assert posted == [], "notice mode must not POST a resolution"


def test_launch_blocked_notice_spawns_notice_popup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``launch_blocked_notice`` pops a ``--notice`` popup (no config/elicitation)."""
    monkeypatch.setattr(native_cost_popup, "_list_tmux_clients", lambda _s, _t: ["/dev/pts/9"])
    spawned: list[list[str]] = []

    class _FakePopen:
        def __init__(self, cmd: list[str], **_kw: Any) -> None:
            spawned.append(cmd)

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    native_cost_popup.launch_blocked_notice(
        "/tmp/x.sock", "main", message="over budget", policy_name="cost-budget"
    )
    assert len(spawned) == 1
    inner = spawned[0][-1]  # the shell-string passed to display-popup
    assert "--notice" in inner and "over budget" in inner
    assert "--config-file" not in inner and "--elicitation-id" not in inner


def test_launch_blocked_notice_skips_without_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No attached client → nothing to render on → no popup spawned."""
    monkeypatch.setattr(native_cost_popup, "_list_tmux_clients", lambda _s, _t: [])
    import subprocess

    def _boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("must not spawn a popup with no client")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    native_cost_popup.launch_blocked_notice("/tmp/x.sock", "main", message="x")
