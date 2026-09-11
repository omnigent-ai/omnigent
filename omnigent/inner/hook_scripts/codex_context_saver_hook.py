"""Pre-execution Context Saver gate for the wrapped Codex harness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from omnigent.runtime.context_saver import (
    ContextSaverAction,
    ContextSaverSettings,
    FocusedReadSettings,
    classify_native_tool_call,
    context_saver_process_available,
    native_redirect_hook_output,
    record_context_saver_event,
)


def evaluate_context_saver_hook(
    payload: object,
    *,
    workspace: Path,
    settings: ContextSaverSettings,
) -> dict[str, object] | None:
    """Return a Codex denial for broad reads, otherwise no hook opinion."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
        return None
    if not settings.enabled or not context_saver_process_available():
        return None
    decision = classify_native_tool_call(
        payload.get("tool_name"),
        payload.get("tool_input"),
        workspace=workspace,
        settings=settings,
    )
    if decision.action is ContextSaverAction.REDIRECT:
        record_context_saver_event(
            decision,
            harness="codex",
            tool_name=str(payload.get("tool_name", "")),
            outcome="redirect",
        )
        return native_redirect_hook_output(decision)
    if decision.action is ContextSaverAction.UNKNOWN:
        record_context_saver_event(
            decision,
            harness="codex",
            tool_name=str(payload.get("tool_name", "")),
            outcome="unknown",
        )
    return None


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="codex-context-saver-hook")
    parser.add_argument("--min-lines", type=_positive_int, required=True)
    parser.add_argument("--focused-read-enabled", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Read one Codex hook payload from stdin and write an optional denial."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(f"omnigent Context Saver hook: malformed JSON: {exc}", file=sys.stderr)
        return 0
    settings = ContextSaverSettings(
        enabled=True,
        techniques={
            "focused_read": FocusedReadSettings(
                enabled=args.focused_read_enabled,
                min_lines=args.min_lines,
            )
        },
    )
    output = evaluate_context_saver_hook(
        payload,
        workspace=Path.cwd(),
        settings=settings,
    )
    if output is not None:
        sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
