"""
Fast ``MessageDisplay`` hook for the native Omnigent Claude wrapper.

Claude Code fires the ``MessageDisplay`` hook once per streamed text
chunk while an assistant message is rendered in the TUI. Claude
**blocks** on command hooks, so this module is deliberately tiny: it
imports only the standard library (no ``httpx``/``claude_native_bridge``
import cost on the per-chunk hot path) and does nothing but append one
structured JSON line to ``<bridge_dir>/message_deltas.jsonl``.

The background transcript forwarder tails that file and turns each line
into a ``response.output_text.delta`` SSE event for the web UI, then
reconciles the live buffer against the authoritative final message item
from the transcript. Keeping HTTP out of this hook is what makes live
streaming smooth — see ``CLAUDE_NATIVE_LIVE_TEXT_STREAMING`` design notes
and :mod:`omnigent.harnesses.claude_native.forwarder`.

Observed ``MessageDisplay`` payload (this Claude build)::

    {
      "hook_event_name": "MessageDisplay",
      "session_id": "<claude-session-uuid>",
      "transcript_path": "/.../<session>.jsonl",
      "cwd": "/path/to/workspace",
      "turn_id": "<turn-uuid>",
      "message_id": "<stable-per-assistant-message-uuid>",
      "index": 7,            # 0-based chunk order within the message
      "final": false,        # true on the last chunk of the message
      "delta": "incremental text for this chunk"
    }

``message_id`` is stable per assistant message and ``delta`` is
incremental (consecutive ``index`` values carry disjoint text), so the
full message is the concatenation of chunks ordered by ``index`` for a
given ``message_id``. Note ``message_id``/``turn_id`` do **not** appear
in Claude's transcript JSONL, so the forwarder/frontend correlate the
live buffer to the final item positionally (FIFO), not by id value.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Append-only deltas file written in the bridge directory. The forwarder
# imports this constant so both sides agree on the path without the
# forwarder paying this module's (stdlib-only) import cost on its hot
# path. Kept here because this module is the writer.
MESSAGE_DELTAS_FILE = "message_deltas.jsonl"


def main(argv: list[str] | None = None) -> int:
    """
    Append one ``MessageDisplay`` chunk to the bridge deltas file.

    :param argv: Optional argv override excluding the program name.
        ``None`` reads :data:`sys.argv`.
    :returns: Process exit code. Always ``0`` — a hook failure must
        never block Claude Code, so malformed input or write errors
        are reported on stderr and swallowed.
    """
    raw_argv = sys.argv[1:] if argv is None else argv
    args = _parse_args(raw_argv)
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        print(f"omnigent message-display hook: malformed JSON: {exc}", file=sys.stderr)
        return 0
    if not isinstance(payload, dict):
        print("omnigent message-display hook: expected JSON object", file=sys.stderr)
        return 0

    record = _delta_record(payload)
    if record is None:
        # Nothing forwardable (e.g. missing message_id or non-string
        # delta). Stay silent on the common no-op rather than spamming
        # stderr for every benign payload shape.
        return 0

    line = json.dumps(record, separators=(",", ":")) + "\n"
    path = os.path.join(args.bridge_dir, MESSAGE_DELTAS_FILE)
    try:
        # O_APPEND makes a single short-line write atomic on POSIX, so
        # concurrent per-chunk hook subprocesses never interleave their
        # lines. ``encode`` once and write the whole buffer in one call.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"omnigent message-display hook: write failed: {exc}", file=sys.stderr)
        return 0
    return 0


def _delta_record(payload: dict[str, object]) -> dict[str, object] | None:
    """
    Extract the forwardable fields from a ``MessageDisplay`` payload.

    :param payload: Hook JSON object read from Claude Code stdin, e.g.
        ``{"hook_event_name": "MessageDisplay", "message_id": "m1",
        "index": 0, "final": false, "delta": "Hello"}``.
    :returns: A compact record ``{"message_id", "index", "final",
        "delta"}`` suitable for the deltas file, or ``None`` when the
        payload lacks a usable ``message_id``/``delta`` pair.
    """
    message_id = payload.get("message_id")
    delta = payload.get("delta")
    if not isinstance(message_id, str) or not message_id:
        return None
    if not isinstance(delta, str):
        return None
    raw_index = payload.get("index")
    # ``index`` orders chunks within a message; treat a missing/invalid
    # value as 0 so a single-chunk message still forwards cleanly.
    index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else 0
    final = bool(payload.get("final"))
    return {
        "message_id": message_id,
        "index": index,
        "final": final,
        "delta": delta,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """
    Parse ``MessageDisplay`` hook arguments.

    :param argv: CLI argv excluding the program name, e.g.
        ``["--bridge-dir", "/tmp/bridge"]``.
    :returns: Parsed argparse namespace with a ``bridge_dir`` attribute.
    """
    parser = argparse.ArgumentParser(
        prog="python -m omnigent.harnesses.claude_native.message_display_hook"
    )
    parser.add_argument("--bridge-dir", required=True)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
