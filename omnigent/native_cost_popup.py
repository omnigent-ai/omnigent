"""
Interactive cost-budget approval prompt for a native terminal.

Runs *inside* a ``tmux display-popup`` overlaid on a native harness's
TUI pane (claude-native / codex-native). It renders a cost-budget
checkpoint to the user, reads an approve/decline answer, and POSTs the
verdict to the Omnigent server's elicitation-resolve endpoint — the **same**
endpoint the web ``ApprovalCard`` uses, so the two surfaces resolve one
shared elicitation Future (whichever answers first wins; the other
clears).

The process is launched by the runner-side popup helper (e.g.
:func:`omnigent.claude_native_bridge.display_cost_approval_popup`) as::

    python -I -m omnigent.native_cost_popup \
        --config-file <bridge_dir>/permission_hook.json \
        --session-id conv_abc123 \
        --elicitation-id elicit_deadbeef \
        --message "Session cost $0.12 crossed the $0.10 checkpoint. Continue?"

AP routing (base URL + auth headers) is read from ``--config-file``
rather than argv so the bearer token never lands in the process list /
tmux command line. The file is the harness's existing AP-routing config
(``permission_hook.json`` for claude-native, ``policy_hook.json`` for
codex-native); both carry ``ap_server_url`` + ``ap_auth_headers``.

Dismissing the popup (Escape / Ctrl-C / EOF) exits **without** posting a
verdict, leaving the elicitation outstanding so it can still be answered
in the web UI or resolved by the server-side approval timeout.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import sys
import threading
import time
from pathlib import Path
from urllib import error, request

# Timeout for the (fast, local) ``tmux list-clients`` lookup the launcher
# runs before popping a modal.
_TMUX_LIST_TIMEOUT_S = 5.0
# Poll interval while waiting for an attaching tmux client to register.
_TMUX_CLIENT_POLL_INTERVAL_S = 0.25
# Poll interval for the watcher that closes the popup when the elicitation
# is resolved on another surface (e.g. answered in the web ApprovalCard).
_RESOLUTION_POLL_INTERVAL_S = 1.5


def wait_for_tmux_client(socket_path: str, tmux_target: str, *, timeout_s: float) -> bool:
    """
    Block until a tmux client is attached to *tmux_target*'s session.

    Used by the runner's terminal-attach path: a client attaches a moment
    *after* the attach starts, so before re-popping a still-pending cost
    approval the caller waits here for the new client to register (else
    :func:`launch_cost_popup` would see zero clients and skip). Synchronous
    (polls with ``time.sleep``); call it via ``asyncio.to_thread``.

    :param socket_path: tmux socket of the pane, e.g.
        ``"/tmp/.../tmux.sock"``.
    :param tmux_target: tmux target of the pane, e.g. ``"main"``.
    :param timeout_s: Max seconds to wait, e.g. ``5.0``.
    :returns: ``True`` once at least one client is attached, ``False`` if
        none attaches within *timeout_s*.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _list_tmux_clients(socket_path, tmux_target):
            return True
        time.sleep(_TMUX_CLIENT_POLL_INTERVAL_S)
    return False


def _list_tmux_clients(socket_path: str, tmux_target: str) -> list[str]:
    """
    List the tmux client names attached to *tmux_target*'s session.

    A ``display-popup`` must render on an attached client and is targeted
    per client (``-c``): the runner that launches it is not itself a tmux
    client, so without ``-c`` tmux fails with "no current client" even
    when a client is attached. Harness-agnostic — used for both
    claude-native and codex-native panes.

    :param socket_path: Absolute path to the tmux socket, e.g.
        ``"/tmp/.../tmux.sock"``.
    :param tmux_target: tmux target whose session's clients to list,
        e.g. ``"main"``.
    :returns: Client names (e.g. ``["/dev/pts/9"]``), or ``[]`` when no
        client is attached or the lookup fails (treated as "nothing to
        render on").
    """
    import subprocess

    try:
        proc = subprocess.run(
            [
                "tmux",
                "-S",
                socket_path,
                "list-clients",
                "-t",
                tmux_target,
                "-F",
                "#{client_name}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_LIST_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def launch_cost_popup(
    socket_path: str,
    tmux_target: str,
    config_file: Path,
    *,
    session_id: str,
    elicitation_id: str,
    message: str,
    policy_name: str | None = None,
    python_executable: str | None = None,
) -> None:
    """
    Overlay the cost-approval modal on every client attached to a pane.

    Harness-agnostic launcher: spawns :func:`main` (this module) inside a
    ``tmux display-popup`` on each tmux client attached to *tmux_target*'s
    session, so a user working in a native terminal — claude-native or
    codex-native — can answer the checkpoint. Each popup resolves the same
    elicitation via the same endpoint the web ApprovalCard uses.

    Fire-and-forget: ``tmux display-popup`` blocks its client until the
    popup closes, so each is spawned **detached** (``Popen``, not awaited).
    When **no client is attached** it skips silently — there is nothing to
    render the modal on, and the web ApprovalCard remains the surface.

    :param socket_path: tmux socket of the pane, e.g.
        ``"/tmp/.../tmux.sock"``.
    :param tmux_target: tmux target of the pane, e.g. ``"main"``.
    :param config_file: AP-routing config the popup reads for
        ``ap_server_url`` + ``ap_auth_headers`` — ``permission_hook.json``
        for claude-native, ``policy_hook.json`` for codex-native.
    :param session_id: Omnigent session id that owns the elicitation, e.g.
        ``"conv_abc123"``. Used in the resolve URL the popup POSTs to.
    :param elicitation_id: Outstanding elicitation correlation id, e.g.
        ``"elicit_deadbeef"``.
    :param message: Approval reason shown in the popup.
    :param policy_name: Name of the deciding policy, rendered as the
        modal header. ``None`` falls back to a generic header.
    :param python_executable: Python used to run the popup module;
        ``None`` uses :data:`sys.executable` (the runner's interpreter,
        valid on the host the tmux server runs on).
    :returns: None.
    """
    import subprocess

    clients = _list_tmux_clients(socket_path, tmux_target)
    if not clients:
        return
    python = python_executable or sys.executable
    argv = [
        python,
        "-I",
        "-m",
        "omnigent.native_cost_popup",
        "--config-file",
        str(config_file),
        "--session-id",
        session_id,
        "--elicitation-id",
        elicitation_id,
        "--message",
        message,
    ]
    if policy_name:
        argv += ["--policy-name", policy_name]
    inner_cmd = shlex.join(argv)
    for client in clients:
        # ``-c`` targets a specific attached client (required: the runner
        # invoking this is not a tmux client). ``-E`` closes the popup when
        # the script exits; ``-w/-h`` are percentages (display-popup +
        # percentage args are tmux >= 3.2). The inner command is one
        # shell-string run via /bin/sh.
        cmd = [
            "tmux",
            "-S",
            socket_path,
            "display-popup",
            "-E",
            "-c",
            client,
            "-t",
            tmux_target,
            "-w",
            "80%",
            "-h",
            "50%",
            inner_cmd,
        ]
        # Detached: do NOT wait (display-popup blocks until answered).
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def launch_blocked_notice(
    socket_path: str,
    tmux_target: str,
    *,
    message: str,
    policy_name: str | None = None,
    python_executable: str | None = None,
) -> None:
    """
    Overlay a dismissable HARD-block notice on every client attached to a pane.

    The DENY counterpart of :func:`launch_cost_popup` — no approve/decline, no
    resolution, just the reason. opencode can only hard-block a prompt by the
    plugin throwing (which opencode renders as a generic "Unexpected server
    error"); this surfaces the policy reason cleanly on the pane so the user
    knows WHY. Reuses the same client-targeting + ``display-popup`` spawn; skips
    silently when no client is attached.

    :param socket_path: tmux socket of the pane.
    :param tmux_target: tmux target of the pane.
    :param message: The block reason shown in the popup.
    :param policy_name: Deciding policy (popup header); ``None`` → generic.
    :param python_executable: Python to run the notice with; ``None`` uses
        :data:`sys.executable`.
    :returns: None.
    """
    import subprocess

    clients = _list_tmux_clients(socket_path, tmux_target)
    if not clients:
        return
    python = python_executable or sys.executable
    argv = [python, "-I", "-m", "omnigent.native_cost_popup", "--notice", "--message", message]
    if policy_name:
        argv += ["--policy-name", policy_name]
    inner_cmd = shlex.join(argv)
    for client in clients:
        cmd = [
            "tmux",
            "-S",
            socket_path,
            "display-popup",
            "-E",
            "-c",
            client,
            "-t",
            tmux_target,
            "-w",
            "80%",
            "-h",
            "50%",
            inner_cmd,
        ]
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _read_omnigent_routing(config_file: Path) -> dict[str, object]:
    """
    Read Omnigent base URL + auth headers from a harness routing-config file.

    :param config_file: Path to the harness's AP-routing config, e.g.
        ``<bridge_dir>/permission_hook.json``. Expected to contain
        ``ap_server_url`` (str) and ``ap_auth_headers`` (dict).
    :returns: The parsed config dict, e.g.
        ``{"ap_server_url": "http://127.0.0.1:8787",
        "ap_auth_headers": {"Authorization": "Bearer ..."}}``.
    :raises SystemExit: If the file is missing, unreadable, not JSON, or
        is missing a usable ``ap_server_url`` — there is no safe default
        for "where is the Omnigent server", so this fails loud (the popup just
        closes and the web card remains the answerable surface).
    """
    try:
        raw = json.loads(config_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise SystemExit(
            f"cost-approval popup: cannot read Omnigent config {config_file}: {exc}"
        ) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("ap_server_url"), str):
        raise SystemExit(
            f"cost-approval popup: Omnigent config {config_file} has no 'ap_server_url' string"
        )
    return raw


def _prompt_verdict(message: str, *, policy_name: str | None = None) -> str | None:
    """
    Render the approval checkpoint and read an approve/decline answer.

    Loops until the user types an unambiguous yes/no. Returns ``None``
    when the user dismisses the prompt (Ctrl-C / EOF / Escape closing
    the popup) so the caller leaves the elicitation outstanding rather
    than synthesizing a verdict the user did not give.

    :param message: The approval reason to show, e.g. ``"Session
        cost $0.12 crossed the $0.10 checkpoint. Continue?"``.
    :param policy_name: Name of the deciding policy, used as the modal
        header so the prompt reflects the actual policy (not a
        hardcoded cost-budget label — this popup serves *any*
        tool-policy ASK). ``None`` falls back to a generic header.
    :returns: ``"accept"`` to continue, ``"decline"`` to stop the
        session, or ``None`` when dismissed without a choice.
    """
    header = f"Policy approval required — {policy_name}" if policy_name else "Approval required"
    print(f"\n  ⚠  {header}\n")
    for line in message.splitlines() or [message]:
        print(f"  {line}")
    print(
        "\n  [y] approve and continue    [n] decline and stop session\n"
        "  (Esc / Ctrl-C to dismiss — you can still answer in the web UI)\n"
    )
    while True:
        try:
            answer = input("  Continue this session? [y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if answer in ("y", "yes"):
            return "accept"
        if answer in ("n", "no"):
            return "decline"
        print("  Please type 'y' or 'n'.")


def _show_notice(message: str, *, policy_name: str | None = None) -> None:
    """
    Render an informational HARD-block notice and wait for dismissal.

    Used for a DENY with no approve option (the prompt is blocked, not gated).
    There is nothing to resolve — just show the reason and block until the user
    presses Enter (or dismisses the popup), then exit so the ``display-popup``
    closes.

    :param message: The block reason, e.g. ``"You've hit the $0.10 budget."``.
    :param policy_name: Deciding policy, used as the header. ``None`` → generic.
    """
    header = f"Blocked by policy — {policy_name}" if policy_name else "Blocked by policy"
    print(f"\n  ⛔  {header}\n")
    for line in message.splitlines() or [message]:
        print(f"  {line}")
    print("\n  This prompt was blocked and not sent to the model.")
    print("\n  Press Enter to dismiss.")
    with contextlib.suppress(EOFError, KeyboardInterrupt):
        input()


def _post_verdict(
    *,
    ap_server_url: str,
    auth_headers: dict[str, str],
    session_id: str,
    elicitation_id: str,
    action: str,
) -> None:
    """
    POST the verdict to the Omnigent elicitation-resolve endpoint.

    Hits ``POST /v1/sessions/{id}/elicitations/{eid}/resolve`` with an
    MCP :class:`~omnigent.server.schemas.ElicitationResult` body — the
    identical call the web ``ApprovalCard`` makes — so the cost
    elicitation's server-side Future resolves the same way regardless of
    which surface answered. Resolving an already-resolved id is a
    server-side no-op, so a race with the web card is harmless.

    :param ap_server_url: Omnigent server base URL, e.g.
        ``"http://127.0.0.1:8787"``.
    :param auth_headers: Outbound auth headers for the session's owner,
        e.g. ``{"Authorization": "Bearer ..."}``. May be empty in
        single-user / local setups.
    :param session_id: Omnigent session id that owns the elicitation, e.g.
        ``"conv_abc123"``.
    :param elicitation_id: Correlation id of the outstanding
        elicitation, e.g. ``"elicit_deadbeef"``.
    :param action: MCP action verb — ``"accept"`` or ``"decline"``.
    :raises SystemExit: On a transport error or non-2xx response, with a
        short message (the popup closes; the web card remains usable).
    """
    url = (
        f"{ap_server_url.rstrip('/')}"
        f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve"
    )
    body = json.dumps({"action": action}).encode("utf-8")
    headers = {"Content-Type": "application/json", **auth_headers}
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        # ``urlopen`` raises ``HTTPError`` for any non-2xx (and unredirected
        # 3xx) status, so a returned response is already a success — there is
        # nothing to inspect on it; we don't need the body.
        with request.urlopen(req, timeout=10.0):
            pass
    except error.HTTPError as exc:
        raise SystemExit(f"cost-approval popup: resolve rejected (HTTP {exc.code})") from exc
    except error.URLError as exc:
        raise SystemExit(f"cost-approval popup: resolve unreachable ({exc.reason})") from exc


def _start_resolution_watcher(
    *,
    ap_server_url: str,
    auth_headers: dict[str, str],
    session_id: str,
    elicitation_id: str,
) -> None:
    """
    Spawn a daemon thread that closes the popup if resolved elsewhere.

    The main thread blocks on :func:`_prompt_verdict` (``input``) waiting
    for y/n, so if the user instead answers in the web ``ApprovalCard`` (or
    the server times the approval out) this popup would otherwise hang open.
    The watcher polls the session snapshot; once the elicitation is no
    longer in ``pending_elicitations`` it force-exits the process, which
    closes the ``tmux display-popup`` (launched with ``-E``).

    :param ap_server_url: Omnigent server base URL, e.g.
        ``"http://127.0.0.1:8787"``.
    :param auth_headers: Outbound auth headers for the session's owner.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Elicitation id to watch, e.g. ``"elicit_x"``.
    :returns: None.
    """
    url = f"{ap_server_url.rstrip('/')}/v1/sessions/{session_id}"

    def _watch() -> None:
        while True:
            time.sleep(_RESOLUTION_POLL_INTERVAL_S)
            req = request.Request(url, headers=auth_headers, method="GET")
            try:
                with request.urlopen(req, timeout=10.0) as resp:
                    snapshot = json.loads(resp.read().decode("utf-8"))
            except (error.URLError, OSError, ValueError):
                continue  # transient (HTTPError is a URLError); keep watching
            pending = snapshot.get("pending_elicitations") or []
            still_pending = any(
                isinstance(e, dict) and e.get("elicitation_id") == elicitation_id for e in pending
            )
            if not still_pending:
                # Resolved on another surface (web card) or timed out — close
                # the popup by exiting the process (``-E`` tears it down).
                os._exit(0)

    threading.Thread(target=_watch, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    """
    Entry point: prompt for a verdict and resolve the elicitation.

    Reads Omnigent routing up front (so a missing/invalid config fails loud
    before prompting), starts a watcher that closes this popup if the
    approval is answered elsewhere, then prompts and POSTs the verdict.

    :param argv: Argument vector excluding the program name; defaults to
        :data:`sys.argv` ``[1:]`` when ``None``.
    :returns: Process exit code — ``0`` on a delivered verdict or a
        clean dismissal, non-zero only via :class:`SystemExit` raised by
        the helpers on a hard failure.
    """
    parser = argparse.ArgumentParser(prog="omnigent.native_cost_popup")
    parser.add_argument(
        "--notice",
        action="store_true",
        help="Informational mode: show the reason for a HARD block and wait for "
        "dismissal — no approve/decline, no server resolution.",
    )
    parser.add_argument("--config-file", help="Path to AP-routing config JSON.")
    parser.add_argument("--session-id", help="AP session id owning the prompt.")
    parser.add_argument("--elicitation-id", help="Outstanding elicitation id.")
    parser.add_argument("--message", required=True, help="Approval / block reason to display.")
    parser.add_argument(
        "--policy-name",
        default=None,
        help="Name of the deciding policy, shown as the modal header.",
    )
    args = parser.parse_args(argv)

    if args.notice:
        # Hard DENY with no approve option (e.g. an opencode cost-budget cap,
        # where the block is enforced by the plugin throwing). Just surface the
        # reason and wait for the user to dismiss — no resolution to POST.
        _show_notice(args.message, policy_name=args.policy_name)
        return 0

    if not (args.config_file and args.session_id and args.elicitation_id):
        parser.error(
            "--config-file / --session-id / --elicitation-id are required without --notice"
        )

    config = _read_omnigent_routing(Path(args.config_file))
    ap_server_url = str(config["ap_server_url"])
    raw_headers = config.get("ap_auth_headers")
    auth_headers = (
        {str(k): str(v) for k, v in raw_headers.items()} if isinstance(raw_headers, dict) else {}
    )
    _start_resolution_watcher(
        ap_server_url=ap_server_url,
        auth_headers=auth_headers,
        session_id=args.session_id,
        elicitation_id=args.elicitation_id,
    )

    action = _prompt_verdict(args.message, policy_name=args.policy_name)
    if action is None:
        # Dismissed: leave the elicitation outstanding for the web UI /
        # the server-side approval timeout.
        print("\n  Dismissed — answer in the web UI or it will time out.\n")
        return 0
    _post_verdict(
        ap_server_url=ap_server_url,
        auth_headers=auth_headers,
        session_id=args.session_id,
        elicitation_id=args.elicitation_id,
        action=action,
    )
    print(
        "\n  Approved — continuing.\n"
        if action == "accept"
        else "\n  Declined — stopping the session.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
