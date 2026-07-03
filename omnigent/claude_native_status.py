"""Claude Code statusLine wrapper that captures context-window data.

Claude Code only exposes ``context_window`` (the model's real window
size and live usage) to the ``statusLine`` command via stdin. This
wrapper reads that stdin, writes the relevant fields atomically to
``bridge_dir/context.json`` for the forwarder to consume, then exec's
the user's pre-existing statusLine command (if any) with the same
stdin so its terminal output still renders.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_CONTEXT_FILE = "context.json"


def main(argv: list[str] | None = None) -> int:
    """
    Capture Claude Code's statusLine stdin and chain to the user's command.

    :param argv: Optional argv override (excluding program name).
    :returns: ``0`` on success, ``0`` after a soft failure (the
        statusLine must never crash Claude Code's TUI loop).
    """
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    raw = sys.stdin.read()
    payload: dict[str, object] | None = None
    try:
        parsed = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        payload = parsed
    if payload is not None:
        _write_context_atomic(Path(args.bridge_dir), payload)
    if args.chain:
        _chain(args.chain, raw)
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """
    Parse wrapper arguments.

    :param argv: CLI argv excluding program name.
    :returns: Parsed namespace with ``bridge_dir`` and optional ``chain``.
    """
    parser = argparse.ArgumentParser(prog="python -m omnigent.claude_native_status")
    parser.add_argument("--bridge-dir", required=True)
    # The user's original statusLine command (as a shell string). When
    # set we exec it with the same stdin so claude-hud / their custom
    # status bar still renders for omnigent-launched sessions.
    parser.add_argument("--chain", default=None)
    return parser.parse_args(argv)


def _write_context_atomic(bridge_dir: Path, payload: dict[str, object]) -> None:
    """
    Persist the statusLine payload's context fields to ``context.json``.

    Atomic write so the forwarder never observes a half-written file.
    Soft-fails (writes nothing) when ``context_window`` is missing or
    malformed — there's nothing useful to record.

    :param bridge_dir: Bridge directory shared with the forwarder.
    :param payload: Decoded statusLine stdin JSON.
    """
    context = payload.get("context_window")
    if not isinstance(context, dict):
        return
    size = context.get("context_window_size")
    usage = context.get("current_usage")
    if not isinstance(size, int) or size <= 0:
        return
    record: dict[str, object] = {"context_window_size": size}
    if isinstance(usage, dict):
        record["current_usage"] = usage
    used_pct = context.get("used_percentage")
    if isinstance(used_pct, (int, float)):
        record["used_percentage"] = used_pct
    # Claude Code's statusLine stdin carries a top-level ``cost`` block with
    # its own cumulative session billing. Capture ``total_cost_usd`` so the
    # forwarder can report it (claude-native produces no ``response.completed``
    # event, so the Omnigent relay's cost accumulation never runs for it).
    cost = payload.get("cost")
    if isinstance(cost, dict):
        total_cost = cost.get("total_cost_usd")
        if (
            isinstance(total_cost, (int, float))
            and not isinstance(total_cost, bool)
            and total_cost >= 0
        ):
            record["total_cost_usd"] = float(total_cost)
    # Claude Code's statusLine stdin carries the active model as a ``model``
    # block (``{"id": "claude-opus-4-8", "display_name": "Opus"}``), rewritten
    # on every render — including right after an in-pane ``/model`` switch.
    # Capture the concrete id so the forwarder can mirror the switch to
    # ``model_override`` on the next poll, before the user's next message,
    # rather than waiting for the next turn's transcript to reveal the model
    # (which lagged model-gated policies by one turn). Defensive about the
    # shape: accept a ``{id|display_name}`` dict or a bare string.
    model = payload.get("model")
    model_id: str | None = None
    if isinstance(model, dict):
        raw_model = model.get("id") or model.get("display_name")
        if isinstance(raw_model, str) and raw_model.strip():
            model_id = raw_model.strip()
    elif isinstance(model, str) and model.strip():
        model_id = model.strip()
    if model_id is not None:
        record["model"] = model_id
    try:
        bridge_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".context-", dir=str(bridge_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, separators=(",", ":"))
            os.replace(tmp_path, str(bridge_dir / _CONTEXT_FILE))
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
    except OSError as exc:
        print(f"omnigent claude status: write failed: {exc}", file=sys.stderr)


def _chain(command: str, stdin_payload: str) -> None:
    """
    Exec the user's pre-existing statusLine command, piping our stdin.

    :param command: Shell command string from the user's
        ``~/.claude/settings.json`` ``statusLine.command``.
    :param stdin_payload: The original stdin we received from Claude
        Code. Forwarded verbatim so the chained command sees exactly
        what Claude Code sent.
    """
    try:
        proc = subprocess.run(
            command,
            input=stdin_payload,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"omnigent claude status: chain failed: {exc}", file=sys.stderr)
        return
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
