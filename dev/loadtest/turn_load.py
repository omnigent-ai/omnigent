#!/usr/bin/env python3
"""Runner-level load test: real multi-turn agent conversations, mocked LLM.

Where ``ws_load_test.py`` measures the control-plane WebSocket fan-out, this
drives **real agent turns through the runner** — the full
``POST .../events`` → server → runner → in-process executor → LLM → stream
back → ``idle`` loop — under concurrency, with the model mocked so the numbers
isolate Omnigent's own dispatch / streaming / history-handling overhead rather
than provider latency.

It runs **N concurrent conversations**, each **M sequential turns** on one
durable session (so history grows across the turns — a real long conversation,
not N one-shots). Every turn's post→idle latency is recorded.

The whole stack is booted locally for the run by reusing the benchmark
harness's :class:`BenchEnvironment` (``dev/benchmarks/omnigent/environment.py``):
a real ``omnigent server`` + a **zero-latency mock LLM** + a sibling **runner**,
with agents wired to the mock. The agent uses the in-process ``openai-agents``
harness (no vendor CLI, no real API key), so a turn is genuine runner work with
a mocked model reply. This means the tool runs **from a repo checkout only**
(it imports ``dev.benchmarks`` and ``tests``), unlike ``ws_load_test.py`` which
hits any deployment.

Concurrency is asyncio (the runner stack is async), not Locust — ``gather`` over
the conversations. Nothing to point ``--server`` at: the stack is created and
torn down by the run.

Metrics (written to ``summary.md`` + ``run_config.json`` like the WS runner):

* ``session create`` — create + bind one session to the runner.
* ``turn`` — one post→idle turn. The headline "a real turn ran" latency; its
  distribution widens across a conversation as history grows.

Run:

    pip install -e '.[dev,agents-sdk]'   # BenchEnvironment + openai-agents harness
    python dev/loadtest/turn_load.py --conversations 10 --turns 8

Knobs: ``--conversations`` (N), ``--turns`` (M per conversation),
``--reply-words`` (mock reply length), ``--turn-timeout``, ``--out-dir``.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import statistics
import sys
import time
from pathlib import Path

# Repo root so ``dev.benchmarks`` and ``tests`` import when run as a script.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Where result directories are created when --out-dir is not given.
_RESULTS_ROOT = Path(__file__).with_name("results")

# A rotating set of user prompts so each turn sends different text; the point is
# to keep the conversation going and its history growing, not to be realistic
# dialogue. Cycled by turn index.
_PROMPTS = [
    "Give me one interesting fact.",
    "Expand on that a little.",
    "How does it connect to what you said before?",
    "Summarize the conversation so far in one line.",
    "Now give me a different fact.",
    "What's a common misconception about it?",
    "Tie it back to the first thing you told me.",
    "What should I read next to learn more?",
]


def _build_parser() -> argparse.ArgumentParser:
    """Build the runner's argument parser."""
    parser = argparse.ArgumentParser(
        description="Runner-level load test: N concurrent M-turn conversations, mocked LLM.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--conversations",
        type=int,
        default=10,
        help="Number of concurrent conversations (N).",
    )
    parser.add_argument(
        "--turns",
        type=int,
        default=8,
        help="Sequential turns per conversation (M) — history grows across them.",
    )
    parser.add_argument(
        "--reply-words",
        type=int,
        default=80,
        help="Word count of the mock assistant reply each turn (streamed).",
    )
    parser.add_argument(
        "--turn-timeout",
        type=float,
        default=180.0,
        help="Per-turn timeout in seconds before a turn is counted as failed.",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Result directory. Default: dev/loadtest/results/turn_load-<timestamp>.",
    )
    return parser


def _resolve_out_dir(out_dir: str | None) -> Path:
    """Resolve (and create) the result directory for this run."""
    if out_dir:
        out = Path(out_dir)
    else:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = _RESULTS_ROOT / f"turn_load-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _mock_reply(word_count: int) -> str:
    """Build a deterministic multi-sentence reply of roughly *word_count* words."""
    sentence = "This is a mocked assistant reply used to exercise the runner turn loop."
    words = sentence.split()
    out: list[str] = []
    while len(out) < word_count:
        out.extend(words)
    return " ".join(out[:word_count]) + "."


class _Metrics:
    """Collects per-operation latencies and failures across the run."""

    def __init__(self) -> None:
        self.create_ms: list[float] = []
        self.turn_ms: list[float] = []
        self.failures: dict[str, int] = {}

    def record_failure(self, reason: str) -> None:
        """Increment the count for one failure *reason* (truncated)."""
        key = reason[:200]
        self.failures[key] = self.failures.get(key, 0) + 1


async def _run_conversation(
    env,
    agent_id: str,
    turns: int,
    turn_timeout: float,
    metrics: _Metrics,
) -> None:
    """Create one bound session and drive *turns* sequential turns on it.

    Records the create latency and each turn's post→idle latency. Stops the
    conversation on the first failed turn (a broken session won't recover), so
    a stall doesn't inflate the sample with timeouts.

    :param env: The booted :class:`BenchEnvironment` (with_runner=True).
    :param agent_id: Registered agent id every conversation shares.
    :param turns: Number of sequential turns (M).
    :param turn_timeout: Per-turn timeout in seconds.
    :param metrics: Shared collector.
    """
    start = time.monotonic()
    try:
        session_id = await env.create_bound_session(agent_id)
    except Exception as exc:  # noqa: BLE001 — record + abandon this conversation
        metrics.record_failure(f"create_session: {exc!r}")
        return
    metrics.create_ms.append((time.monotonic() - start) * 1000)

    for i in range(turns):
        prompt = _PROMPTS[i % len(_PROMPTS)]
        turn_start = time.monotonic()
        try:
            await env.drive_turn(session_id, prompt, timeout=turn_timeout)
        except Exception as exc:  # noqa: BLE001 — record; stop this conversation
            metrics.record_failure(f"turn: {exc!r}")
            return
        metrics.turn_ms.append((time.monotonic() - turn_start) * 1000)


def _pct(values: list[float], p: float) -> float:
    """Return the *p*-th percentile (ceil-index) of *values*, or 0.0 if empty."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int((p / 100) * len(ordered) + 0.999999) - 1))
    return ordered[idx]


def _row(name: str, values: list[float], fails: int, wall_s: float) -> str:
    """Format one metric row for the summary table."""
    if not values:
        return f"| {name} | 0 | {fails} | - | - | - | - | - | - |"
    avg = statistics.mean(values)
    med = statistics.median(values)
    rps = len(values) / wall_s if wall_s > 0 else 0.0
    return (
        f"| {name} | {len(values)} | {fails} | {avg:.1f} | {med:.1f} | "
        f"{_pct(values, 95):.1f} | {_pct(values, 99):.1f} | {max(values):.1f} | {rps:.1f} |"
    )


def _write_results(
    out_dir: Path,
    args: argparse.Namespace,
    metrics: _Metrics,
    wall_s: float,
    booted: bool,
) -> None:
    """Write run_config.json + a human-readable summary.md.

    :param out_dir: Result directory.
    :param args: Parsed CLI arguments.
    :param metrics: Collected latencies/failures.
    :param wall_s: Total drive wall-time (seconds), for req/s.
    :param booted: Whether the stack booted (False = summary reports the boot
        failure instead of empty tables).
    """
    total_turns = len(metrics.turn_ms)
    total_fails = sum(metrics.failures.values())
    config = {
        "scenario": "turn_load",
        "conversations": args.conversations,
        "turns_per_conversation": args.turns,
        "reply_words": args.reply_words,
        "turn_timeout_s": args.turn_timeout,
        "harness": "openai-agents",
        "model": "mock (zero-latency)",
        "booted": booted,
        "turns_completed": total_turns,
        "failures": total_fails,
        "wall_time_s": round(wall_s, 3),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    (out_dir / "run_config.json").write_text(json.dumps(config, indent=2))

    lines: list[str] = []
    lines.append("# Load test results — turn_load (runner + mocked LLM)")
    lines.append("")
    lines.append(
        f"- **Load:** {args.conversations} concurrent conversations × "
        f"{args.turns} turns each = {args.conversations * args.turns} turns"
    )
    lines.append(f"- **Mock reply:** ~{args.reply_words} words, streamed")
    lines.append("- **Harness:** openai-agents (in-process); model mocked, zero-latency")
    if not booted:
        lines.append("- **Outcome:** FAILED TO BOOT — see console.log")
        (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
        return
    outcome = "PASS — no failures" if total_fails == 0 else f"FAIL — {total_fails} failure(s)"
    lines.append(f"- **Outcome:** {outcome}")
    lines.append("")
    lines.append(f"**{total_turns} turns completed, {total_fails} failures.**")
    lines.append("")
    lines.append("## Latency (ms)")
    lines.append("")
    lines.append("| Op | # | Fails | Avg | Med | p95 | p99 | Max | Ops/s |")
    lines.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    lines.append(_row("session create", metrics.create_ms, 0, wall_s))
    lines.append(_row("turn", metrics.turn_ms, total_fails, wall_s))
    lines.append("")

    if metrics.failures:
        lines.append("## Failures")
        lines.append("")
        lines.append("| Reason | Count |")
        lines.append("|---|--:|")
        for reason, count in sorted(metrics.failures.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {reason.replace('|', chr(92) + '|')} | {count} |")
        lines.append("")

    lines.append("## Reading the numbers")
    lines.append("")
    lines.append(
        "- **turn** is the headline: one full post→idle agent turn through the "
        "runner (server → runner → executor → mocked LLM → stream → idle). With "
        "the model mocked at zero latency, this is Omnigent's own per-turn "
        "overhead, not provider time."
    )
    lines.append(
        "- Turn latency **grows across a conversation** as history accumulates "
        "(each turn re-sends the growing context), so the tail (p95/p99/max) "
        "reflects the later, longer turns — watch it as M rises."
    )
    lines.append(
        "- **session create** is the create + runner-bind cost, paid once per " "conversation."
    )
    lines.append(
        "- **Ops/s** is throughput at this concurrency; a single runner services "
        "all N conversations, so it is the runner's concurrent turn throughput."
    )
    lines.append("")
    lines.append("Raw config: `run_config.json`. Stack logs (if any): `console.log`.")
    lines.append("")
    (out_dir / "summary.md").write_text("\n".join(lines))


async def _drive(env, args: argparse.Namespace, metrics: _Metrics) -> float:
    """Register the agent, set the mock reply, and fan out the conversations.

    :returns: Wall-time (seconds) spent driving conversations.
    """
    # A streamed multi-sentence reply for every turn (queue-independent fallback),
    # so each turn does real streaming-pipeline work with a mocked model.
    await env.set_mock_fallback(_mock_reply(args.reply_words), stream=True)
    agent_name = await env.ensure_agent("turnload-agent")
    agent_id = await env.agent_id(agent_name)

    start = time.monotonic()
    await asyncio.gather(
        *(
            _run_conversation(env, agent_id, args.turns, args.turn_timeout, metrics)
            for _ in range(args.conversations)
        )
    )
    return time.monotonic() - start


async def _main_async(args: argparse.Namespace, out_dir: Path) -> int:
    """Boot the stack, drive the load, write results. Returns an exit code."""
    from dev.benchmarks.omnigent.environment import BenchEnvironment

    metrics = _Metrics()
    print(
        f"Booting local stack (server + mock LLM + runner) for "
        f"{args.conversations}×{args.turns} turns …",
        flush=True,
    )
    try:
        async with BenchEnvironment(with_runner=True) as env:
            print(f"  stack up: {env.base_url}", flush=True)
            wall_s = await _drive(env, args, metrics)
    except Exception as exc:  # noqa: BLE001 — report boot/drive failure in the result set
        print(f"\nERROR: {exc!r}", flush=True)
        _write_results(out_dir, args, metrics, 0.0, booted=False)
        return 1

    _write_results(out_dir, args, metrics, wall_s, booted=True)
    print(f"\nResults written to {out_dir}", flush=True)
    print(f"  summary: {out_dir / 'summary.md'}", flush=True)
    return 1 if sum(metrics.failures.values()) else 0


def main() -> int:
    """CLI entrypoint."""
    args = _build_parser().parse_args()
    out_dir = _resolve_out_dir(args.out_dir)
    return asyncio.run(_main_async(args, out_dir))


if __name__ == "__main__":
    raise SystemExit(main())
