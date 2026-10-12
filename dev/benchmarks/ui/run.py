"""Benchmark the built OSS UI with real keyboard input and Chromium rendering.

    uv run --no-sync dev/benchmarks/ui/run.py --output-dir artifacts/ui-benchmark

An optional --baseline-dist compares two production bundles on the same host,
alternating their order between runs. Both use this checkout's OSS backend and
the same fixture and browser; no runner, model, or credentials are needed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import json
import math
import signal
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from playwright.async_api import (
    Browser,
    CDPSession,
    Page,
    Request,
    Response,
    async_playwright,
    expect,
)

from dev.benchmarks.omnigent.compare import (
    build_markdown,
    compare_reports,
)
from dev.benchmarks.omnigent.environment import BenchEnvironment
from dev.benchmarks.omnigent.measure import RunResult, aggregate
from dev.benchmarks.omnigent.schema import build_report, git_sha

_ROOT = Path(__file__).resolve().parents[3]
_DIST = _ROOT / "omnigent/server/static/web-ui"
_PROBE = Path(__file__).with_name("probe.js").read_text()
_MODES = ("browser", "mac_css")
_METRICS = ("session_open", "key_to_frame", "style_layout")
_TEXT = "Please review the changes and explain how the interface stays responsive. "
_MIN_ELEMENTS = 2500
_TRANSCRIPT_TURNS = 12
_TABLE_ROWS = 100


class UIEnvironment(BenchEnvironment):
    """The shared server lifecycle, with explicit UI assets and disposable state."""

    def __init__(self, dist: Path) -> None:
        super().__init__()
        self.dist = dist

    def child_env(self) -> dict[str, str]:
        return {
            **super().child_env(),
            "OMNIGENT_WEB_UI_DIST": str(self.dist),
            "OMNIGENT_DATA_DIR": str(self._tmp / "data"),
            "OMNIGENT_CONFIG_HOME": str(self._tmp / "config"),
        }


async def seed_conversation(env: BenchEnvironment) -> str:
    """Use the public history-ingestion API; setup is outside every timed region."""
    assert env.client is not None
    name = await env.ensure_agent("ui-benchmark")
    session_id = await env.create_session(await env.agent_id(name))
    table = "| File | Status | Description |\n| --- | --- | --- |\n" + "\n".join(
        f"| `module_{i}.py` | **Reviewed** | A [reference](https://example.com) and explanation. |"
        for i in range(_TABLE_ROWS)
    )
    for turn in range(_TRANSCRIPT_TURNS):
        for role, text in (
            ("user", f"Review batch {turn} and summarize the changes."),
            ("assistant", f"## Review batch {turn}\n\n{table}\n\nReview complete {turn}."),
        ):
            response = await env.client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "external_conversation_item",
                    "data": {
                        "item_type": "message",
                        "response_id": f"review-{turn}",
                        "item_data": {
                            "role": role,
                            "agent": name if role == "assistant" else None,
                            "content": [
                                {
                                    "type": "output_text" if role == "assistant" else "input_text",
                                    "text": text,
                                }
                            ],
                        },
                    },
                },
            )
            response.raise_for_status()
    return session_id


async def _metrics(cdp: CDPSession) -> dict[str, float]:
    data = await cdp.send("Performance.getMetrics")
    return {item["name"]: item["value"] for item in data["metrics"]}


async def measure_typing(page: Page, cdp: CDPSession, count: int) -> dict[str, Any]:
    """Measure in the renderer, excluding Python/Playwright transport latency."""
    composer = page.get_by_role("textbox", name="Message the agent", exact=True)
    await composer.fill("")
    await composer.focus()
    await page.evaluate(_PROBE)
    await page.evaluate(
        "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
    )
    frame_ms, render_ms, script_ms = [], [], []
    expected = ""
    started = time.perf_counter()
    for index in range(count):
        character = _TEXT[index % len(_TEXT)]
        await composer.evaluate("el => window.omnigentTypingBenchmark.arm(el)")
        before = await _metrics(cdp)
        await page.keyboard.press("Space" if character == " " else character)
        sample = await page.evaluate("() => window.omnigentTypingBenchmark.read()")
        after = await _metrics(cdp)
        expected += character
        if sample["value"] != expected:
            raise RuntimeError(f"Composer lost or changed keystroke {index + 1}")
        frame_ms.append(sample["milliseconds"])
        render_ms.append(
            1000
            * sum(after[key] - before[key] for key in ("RecalcStyleDuration", "LayoutDuration"))
        )
        script_ms.append(1000 * (after["ScriptDuration"] - before["ScriptDuration"]))
        if any(
            not math.isfinite(value) or value < 0
            for value in (frame_ms[-1], render_ms[-1], script_ms[-1])
        ):
            raise RuntimeError("Chromium returned an invalid timing sample")
    await expect(composer).to_have_value(expected)
    return {
        "key_to_frame": frame_ms,
        "style_layout": render_ms,
        "script_ms": script_ms,
        "wall_time_s": time.perf_counter() - started,
    }


async def measure_scenario(
    browser: Browser,
    env: BenchEnvironment,
    session_id: str,
    mode: str,
    args: argparse.Namespace,
    evidence: Path,
) -> dict[str, Any]:
    context = await browser.new_context(viewport={"width": 1440, "height": 900})
    page = await context.new_page()
    errors: list[str] = []

    def check_response(response: Response) -> None:
        if response.request.resource_type in {"document", "script", "stylesheet", "font"}:
            if response.status >= 400:
                errors.append(
                    f"Required asset ({response.request.resource_type}) returned "
                    f"HTTP {response.status}: {response.url}"
                )

    def check_request(request: Request) -> None:
        if request.resource_type in {"document", "script", "stylesheet", "font"}:
            errors.append(
                f"Required asset ({request.resource_type}) failed: "
                f"{request.url}: {request.failure}"
            )

    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", check_response)
    page.on("requestfailed", check_request)
    try:
        cdp = await context.new_cdp_session(page)
        await cdp.send("Performance.enable")
        await cdp.send("Emulation.setCPUThrottlingRate", {"rate": args.cpu_throttle})
        if mode == "mac_css":
            # Apply the desktop CSS scope before navigation's first render.
            await page.add_init_script("""(() => {
                const mark = () => {
                    if (!document.documentElement) return false;
                    document.documentElement.dataset.electronMac = 'true';
                    return true;
                };
                if (!mark()) {
                    const observer = new MutationObserver(() => {
                        if (mark()) observer.disconnect();
                    });
                    observer.observe(document, {childList: true});
                }
            })()""")
        opened = time.perf_counter()
        await page.goto(f"{env.base_url}/c/{session_id}", wait_until="domcontentloaded")
        if errors:
            raise RuntimeError(f"Browser errors: {errors}")
        composer = page.get_by_role("textbox", name="Message the agent", exact=True)
        await expect(composer).to_be_editable(timeout=30_000)
        await expect(
            page.get_by_text(f"Review complete {_TRANSCRIPT_TURNS - 1}.", exact=True)
        ).to_be_visible(timeout=30_000)
        await page.evaluate("() => document.fonts.ready")
        open_ms = (time.perf_counter() - opened) * 1000
        if errors:
            raise RuntimeError(f"Browser errors: {errors}")
        if mode == "mac_css":
            await expect(page.locator("html")).to_have_attribute("data-electron-mac", "true")
        elements = await page.locator("*").count()
        if elements < _MIN_ELEMENTS:
            raise RuntimeError(f"Fixture has only {elements} elements; requires {_MIN_ELEMENTS}")
        await measure_typing(page, cdp, args.warmup)
        sample = await measure_typing(page, cdp, args.iterations)
        elements_after = await page.locator("*").count()
        if elements_after < _MIN_ELEMENTS:
            raise RuntimeError(f"Fixture shrank to {elements_after} elements while typing")
        sample.update(
            session_open=[open_ms],
            dom_elements=elements,
            dom_elements_after=elements_after,
            page_errors=list(errors),
        )
        if errors:
            raise RuntimeError(f"Browser errors: {errors}")
        await page.screenshot(path=str(evidence.with_suffix(".png")))
        return sample
    except Exception:
        with contextlib.suppress(Exception):
            await page.screenshot(path=str(evidence.with_suffix(".failed.png")))
        raise
    finally:
        await context.close()


def required_journeys() -> list[str]:
    return [f"{mode}_{metric}" for mode in _MODES for metric in _METRICS]


def make_report(
    samples: dict[str, list[dict[str, Any]]],
    args: argparse.Namespace,
    revision: str,
    browser_version: str,
) -> dict[str, Any]:
    journeys: dict[str, dict[str, object]] = {}
    for mode in _MODES:
        for metric in _METRICS:
            results = [
                RunResult(
                    latencies_ms=sample[metric],
                    wall_time=sample[metric][0] / 1000
                    if metric == "session_open"
                    else sample["wall_time_s"],
                )
                for sample in samples[mode]
            ]
            journeys[f"{mode}_{metric}"] = {
                **aggregate(results),
                "kind": "latency",
                "backend": "chromium",
                "needs_runner": False,
            }
    report = build_report(
        journeys,
        generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        config={
            "iterations": args.iterations,
            "runs": args.runs,
            "warmup": args.warmup,
            "cpu_throttle": args.cpu_throttle,
            "viewport": {"width": 1440, "height": 900},
            "browser_version": browser_version,
            "transcript_turns": _TRANSCRIPT_TURNS,
            "table_rows": _TABLE_ROWS,
            "server_git_sha": git_sha(),
        },
        harness="chromium-ui",
    )
    report["git_sha"] = revision
    # A supplied bundle revision does not identify its branch.
    report["git_branch"] = ""
    report["samples"] = samples
    return report


def measurement_failures(report: dict[str, Any], args: argparse.Namespace) -> list[str]:
    """Validate both sides before medians can hide missing or invalid samples."""
    failures = []
    for name in required_journeys():
        rows = report.get("journeys", {}).get(name, {}).get("runs", [])
        count = 1 if name.endswith("session_open") else args.iterations
        if len(rows) != args.runs or any(
            row.get("n_success") != count or row.get("n_failures") != 0 for row in rows
        ):
            failures.append(f"{name}: incomplete samples")
        elif any(
            not isinstance(value := row.get(metric), (int, float))
            or not math.isfinite(value)
            or value < 0
            for row in rows
            for metric in ("p50_ms", "p95_ms")
        ):
            failures.append(f"{name}: invalid timing samples")
    return failures


def _budgets(args: argparse.Namespace) -> dict[str, float]:
    return {
        f"{mode}_{metric}": limit
        for mode in _MODES
        for metric, limit in (
            ("key_to_frame", args.max_key_to_frame_ms),
            ("style_layout", args.max_style_layout_ms),
        )
    }


def budget_failures(report: dict[str, Any], args: argparse.Namespace) -> list[str]:
    """Use run-median P95 after checking measurement completeness."""
    failures = []
    for name, limit in _budgets(args).items():
        value = statistics.median(row["p95_ms"] for row in report["journeys"][name]["runs"])
        if value > limit:
            failures.append(f"{name}: P95 {value:.2f} ms exceeds {limit:.2f} ms")
    return failures


def assess_reports(
    reports: dict[str, dict[str, Any]], args: argparse.Namespace
) -> tuple[list[str], list[str], list[dict]]:
    """Only block paired runs on substantial, repeated, over-budget slowdowns."""
    failures = [
        f"{variant}: {failure}"
        for variant, report in reports.items()
        for failure in measurement_failures(report, args)
    ]
    if failures:
        return failures, [], []
    candidate = reports["candidate"]
    over_budget = budget_failures(candidate, args)
    if "baseline" not in reports:
        return over_budget, [], []
    if args.runs < 3:
        return ["Paired comparisons require at least three runs"], [], []

    baseline = reports["baseline"]
    _, rows = compare_reports(baseline, candidate, threshold=args.threshold)
    warnings = [f"{message} (advisory with a baseline)" for message in over_budget]
    budgets = _budgets(args)
    required_pairs = args.runs // 2 + 1

    def regressed(base: float, current: float) -> bool:
        return current > base * (1 + args.threshold) and current - base > args.min_regression_ms

    for row in rows:
        name = row["journey"]
        metrics = ["p50", "p95"] if row["p95_gated"] else ["p50"]
        regressing_metrics = [m for m in metrics if regressed(row[f"b_{m}"], row[f"c_{m}"])]
        pairs = list(
            zip(
                baseline["journeys"][name]["runs"],
                candidate["journeys"][name]["runs"],
                strict=True,
            )
        )
        row["regressing_pairs"] = {
            m: sum(regressed(b[f"{m}_ms"], c[f"{m}_ms"]) for b, c in pairs) for m in metrics
        }
        row["status"] = "ok"
        if not regressing_metrics:
            continue
        if name in budgets and row["c_p95"] <= budgets[name]:
            reason = "within the candidate P95 budget"
        elif not any(row["regressing_pairs"][m] >= required_pairs for m in regressing_metrics):
            reason = f"slowdown did not repeat in {required_pairs}/{args.runs} paired runs"
        else:
            row["status"] = "regression"
            failures.append(f"{name}: substantial slowdown confirmed in a majority of paired runs")
            continue
        row["status"] = "advisory"
        warnings.append(f"{name}: {reason}")
    return failures, warnings, rows


async def run_benchmark(args: argparse.Namespace) -> bool:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    variants = {"candidate": (args.web_dist, args.revision)}
    if args.baseline_dist:
        variants = {"baseline": (args.baseline_dist, args.baseline_revision), **variants}
    samples: dict[str, dict[str, list[dict[str, Any]]]] = {
        variant: {mode: [] for mode in _MODES} for variant in variants
    }
    environments = {}
    reports = {}
    async with contextlib.AsyncExitStack() as stack:
        playwright = await stack.enter_async_context(async_playwright())
        browser = await playwright.chromium.launch()
        browser_version = browser.version
        stack.push_async_callback(browser.close)
        for variant, (dist, _) in variants.items():
            env = await stack.enter_async_context(UIEnvironment(dist))
            environments[variant] = (env, await seed_conversation(env))
        for run in range(args.runs):
            order = list(variants) if run % 2 == 0 else list(reversed(variants))
            for variant in order:
                env, session_id = environments[variant]
                for mode in _MODES:
                    print(f"{variant}: {mode}, run {run + 1}/{args.runs}", flush=True)
                    evidence = args.output_dir / f"{variant}-{mode}-{run + 1}"
                    sample = await measure_scenario(browser, env, session_id, mode, args, evidence)
                    samples[variant][mode].append(sample)
                    # Keep partial measurements if a later scenario fails or is cancelled.
                    report = make_report(
                        samples[variant], args, variants[variant][1], browser_version
                    )
                    reports[variant] = report
                    (args.output_dir / f"{variant}.json").write_text(
                        json.dumps(report, indent=2) + "\n"
                    )
    failures, warnings, rows = assess_reports(reports, args)
    summary = [
        "# OSS UI benchmark",
        "",
        f"Chromium {browser_version}; CPU throttle {args.cpu_throttle}x.",
        "",
    ]
    for name, journey in reports["candidate"]["journeys"].items():
        if not journey["runs"]:
            continue
        p50 = statistics.median(row["p50_ms"] for row in journey["runs"])
        p95 = statistics.median(row["p95_ms"] for row in journey["runs"])
        summary.append(f"- `{name}`: run-median P50 {p50:.2f} ms, P95 {p95:.2f} ms")
    summary.extend(
        [
            "",
            f"Candidate P95 budgets: {args.max_key_to_frame_ms:g} ms key-to-frame; "
            f"{args.max_style_layout_ms:g} ms style/layout.",
            "",
        ]
    )
    if "baseline" in reports:
        summary.append(
            f"Blocking regressions require more than {args.threshold:.0%} and "
            f"{args.min_regression_ms:g} ms deterioration in the run median and a majority "
            "of paired runs. Typing must also exceed its candidate P95 budget. "
            "Absolute budget overruns alone are advisory with a baseline.\n"
        )
        summary.append(build_markdown(rows, args.threshold, not failures))
        (args.output_dir / "comparison.json").write_text(
            json.dumps(
                {"passed": not failures, "rows": rows, "failures": failures, "warnings": warnings},
                indent=2,
            )
            + "\n"
        )
    summary.extend(
        [
            "",
            *(f"- Advisory: {warning}" for warning in warnings),
            *(f"- Failure: {failure}" for failure in failures),
            "",
            "FAIL" if failures else "PASS",
        ]
    )
    markdown = "\n".join(summary) + "\n"
    (args.output_dir / "summary.md").write_text(markdown)
    print(markdown)
    return not failures


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-dist", type=Path, default=_DIST)
    parser.add_argument("--revision", default=git_sha())
    parser.add_argument("--baseline-dist", type=Path)
    parser.add_argument("--baseline-revision", default="")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/ui-benchmark"))
    parser.add_argument("--iterations", type=_positive_int, default=80)
    parser.add_argument("--runs", type=_positive_int, default=3)
    parser.add_argument("--warmup", type=_positive_int, default=10)
    parser.add_argument("--cpu-throttle", type=_positive_float, default=4)
    parser.add_argument("--threshold", type=_positive_float, default=1.0)
    parser.add_argument("--min-regression-ms", type=_positive_float, default=5.0)
    parser.add_argument("--max-key-to-frame-ms", type=_positive_float, default=100.0)
    parser.add_argument("--max-style-layout-ms", type=_positive_float, default=16.0)
    args = parser.parse_args(argv)
    if args.cpu_throttle < 1:
        parser.error("--cpu-throttle must be at least 1")
    if args.baseline_dist and not args.baseline_revision:
        parser.error("--baseline-dist requires --baseline-revision for report provenance")
    if args.baseline_dist and args.runs < 3:
        parser.error("--baseline-dist requires at least three --runs")
    for name in ("web_dist", "baseline_dist"):
        dist = getattr(args, name)
        if dist is not None:
            dist = dist.resolve()
            if not (dist / "index.html").is_file():
                parser.error(f"No built SPA at {dist}; run `pnpm --filter web run build` first")
            setattr(args, name, dist)
    return args


async def _run_until_terminated(args: argparse.Namespace) -> bool:
    """Cancel on SIGTERM so browser and server contexts finish their teardown."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    cancelled = False

    def terminate() -> None:
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            task.cancel()

    previous_handler = signal.getsignal(signal.SIGTERM)
    loop.add_signal_handler(signal.SIGTERM, terminate)
    try:
        return await run_benchmark(args)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return 0 if asyncio.run(_run_until_terminated(args)) else 1
    except BaseException as exc:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "error.txt").write_text(f"{type(exc).__name__}: {exc}\n")
        if isinstance(exc, asyncio.CancelledError):
            return 128 + signal.SIGTERM
        raise


if __name__ == "__main__":
    raise SystemExit(main())
