#!/usr/bin/env python3
"""qwen_doctor: standalone, isolated health check for the qwen harness.

Answers one question fast, without redoing the forensics from scratch:
**is the qwen integration currently healthy, and if not, which known failure
mode is it?**

Born from a real debugging session (see docs/QWEN_FOLLOWUPS.md § "Provider
routing: settings.json precedence") that took ~20 tool calls to pin down:
a dead pytest fixture, a never-captured snapshot, and — the actual bug — an
ambient ``~/.qwen/settings.json`` silently hijacking gateway-routed sessions
to a live Databricks model instead of the intended endpoint. This script
re-runs that whole diagnosis in seconds, standalone (no pytest, no fixtures,
no dependency on ambient ``~/.omnigent/config.yaml`` state), so a future
qwen CLI upgrade or config drift can be triaged in one command instead of
another multi-hour session.

Usage::

    uv run python scripts/qwen_doctor.py                # run all checks
    uv run python scripts/qwen_doctor.py --capture       # also refresh the golden file

Exit code 0 iff every REQUIRED check passes. The ambient-settings canary
(D) is informational — it reports your machine's current exposure, it
never fails the run, since a hijacking ~/.qwen/settings.json is a fact
about your machine, not a code bug (that's exactly what the isolation
fix in QwenExecutor exists to route around).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_PATH = Path(__file__).resolve().parent / "qwen_doctor_golden.json"

sys.path.insert(0, str(REPO_ROOT))

CANNED_TEXT = "Hello there, how are you today?"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    required: bool = True
    heal_hint: str = ""


@dataclass
class Report:
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        self.checks.append(result)
        status = "PASS" if result.ok else ("WARN" if not result.required else "FAIL")
        print(f"[{status}] {result.name}: {result.detail}")
        if not result.ok and result.heal_hint:
            print(f"       heal: {result.heal_hint}")

    @property
    def healthy(self) -> bool:
        return all(c.ok for c in self.checks if c.required)


class _MockServer:
    """Spawns tests/server/integration/mock_llm_server.py, kills it on exit."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self._proc: subprocess.Popen | None = None
        self._log = open("/tmp/qwen_doctor_mock_llm.log", "w")  # noqa: SIM115

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [
                sys.executable,
                str(REPO_ROOT / "tests" / "server" / "integration" / "mock_llm_server.py"),
                str(self.port),
            ],
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        import httpx

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.base_url}/stats", timeout=1.0).status_code == 200:
                    return
            except httpx.ConnectError:
                time.sleep(0.1)
        raise RuntimeError("mock LLM server did not start within 10s")

    def configure(self, model: str, text: str) -> None:
        import httpx

        httpx.post(f"{self.base_url}/mock/reset", timeout=5).raise_for_status()
        httpx.post(
            f"{self.base_url}/mock/configure",
            json={"key": model, "responses": [{"text": text}]},
            timeout=5,
        ).raise_for_status()

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.send_signal(signal.SIGTERM)
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._log.close()


def _check_qwen_on_path(report: Report, golden: dict) -> str | None:
    """Check A: qwen binary present and runnable. Returns the version string."""
    path = shutil.which("qwen")
    if path is None:
        report.add(
            CheckResult(
                "A. qwen on PATH",
                ok=False,
                detail="'qwen' not found on PATH",
                heal_hint="Install qwen-code (npm i -g @qwen-code/qwen-code or your usual "
                "method).",
            )
        )
        return None
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15)
        version = proc.stdout.strip() or proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.add(
            CheckResult(
                "A. qwen on PATH",
                ok=False,
                detail=f"'qwen --version' failed: {exc}",
                heal_hint="qwen binary exists but won't run; check the install / node runtime.",
            )
        )
        return None

    golden_version = golden.get("qwen_version")
    drift_note = ""
    if golden_version and golden_version != version:
        drift_note = (
            f" (drifted from last-known-good {golden_version!r} — not necessarily a "
            "problem, just noting it)"
        )
    report.add(
        CheckResult(
            "A. qwen on PATH",
            ok=True,
            detail=f"{path} -> {version}{drift_note}",
        )
    )
    return version


async def _check_raw_acp_handshake(report: Report) -> bool:
    """Check B: qwen --acp still speaks the protocol shape omnigent expects.

    Bypasses QwenExecutor entirely — talks raw JSON-RPC over stdin/stdout —
    so a failure here means "the qwen CLI's own ACP protocol changed",
    distinct from "omnigent's executor code regressed" (check C).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "qwen",
            "--acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
    except OSError as exc:
        report.add(
            CheckResult(
                "B. raw ACP handshake", ok=False, detail=f"could not spawn qwen --acp: {exc}"
            )
        )
        return False

    async def rpc(msg_id: int, method: str, params: dict) -> dict:
        req = (
            json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}) + "\n"
        )
        proc.stdin.write(req.encode())
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=20)
        return json.loads(line)

    try:
        init_resp = await rpc(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        has_caps = "agentCapabilities" in init_resp.get("result", {})
        new_resp = await rpc(2, "session/new", {"cwd": str(REPO_ROOT), "mcpServers": []})
        session_id = new_resp.get("result", {}).get("sessionId")
        ok = has_caps and bool(session_id)
        report.add(
            CheckResult(
                "B. raw ACP handshake",
                ok=ok,
                detail=(
                    "initialize + session/new returned expected shape"
                    if ok
                    else f"unexpected shape: init={init_resp!r}, new={new_resp!r}"[:300]
                ),
                heal_hint="qwen's ACP protocol changed shape; diff against "
                "docs/QWEN_NATIVE_DESIGN.md § Protocol surface and update the executors.",
            )
        )
        return ok
    except (asyncio.TimeoutError, json.JSONDecodeError) as exc:
        report.add(
            CheckResult(
                "B. raw ACP handshake",
                ok=False,
                detail=f"handshake failed: {exc}",
                heal_hint="qwen --acp isn't responding as expected; run it by hand to see "
                "raw output.",
            )
        )
        return False
    finally:
        with contextlib.suppress(Exception):
            # Explicitly close stdin's transport via the public StreamWriter
            # API before the process/transport objects go out of scope --
            # otherwise their __del__ can fire after asyncio.run() has
            # already closed the loop and print a spurious (harmless)
            # "Event loop is closed" traceback.
            proc.stdin.close()
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)


async def _check_isolated_gateway_routing(report: Report, mock: _MockServer, model: str) -> bool:
    """Check C: the actual regression check for the HOME-isolation fix.

    Drives the real omnigent.inner.qwen_executor.QwenExecutor (not a
    hand-rolled reimplementation) with a gateway pointed at the mock server.
    If this ever fails again, the isolation fix regressed or qwen changed how
    it discovers config.
    """
    from omnigent.inner.executor import ExecutorError, TurnComplete
    from omnigent.inner.qwen_executor import QwenExecutor

    executor = QwenExecutor(
        model=model,
        gateway_base_url=f"{mock.base_url}/v1",
        gateway_auth_command="echo mock-key",
    )
    response_text = ""
    error_text = ""
    try:
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "say hi in 5 words"}],
            tools=[],
            system_prompt="",
        ):
            if isinstance(event, TurnComplete):
                response_text = event.response
            elif isinstance(event, ExecutorError):
                error_text = event.message
    finally:
        await executor.close()

    ok = response_text.strip() == CANNED_TEXT
    report.add(
        CheckResult(
            "C. isolated gateway routing (QwenExecutor)",
            ok=ok,
            detail=(
                f"got expected mock response {response_text!r}"
                if ok
                else f"expected {CANNED_TEXT!r}, got response={response_text!r} "
                f"error={error_text!r}"
            ),
            heal_hint="The HOME-isolation fix in QwenExecutor._isolated_home_dir "
            "regressed, or qwen changed how/where it reads config. Re-diff against "
            "docs/QWEN_FOLLOWUPS.md § 'Provider routing: settings.json precedence'.",
        )
    )
    return ok


async def _check_ambient_settings_canary(report: Report, mock: _MockServer, model: str) -> None:
    """Check D (informational, never fails the run): does this machine's
    ambient ~/.qwen/settings.json actually hijack an *unisolated* gateway
    request right now?

    Runs the identical request as check C but through a bare, unisolated
    subprocess (real ambient $HOME) instead of QwenExecutor. If this canary
    shows a hijack while check C (isolated) passes, that's the exact
    signature this session diagnosed — and it's proof the isolation fix is
    doing real, currently-necessary work on this machine, not a no-op.
    """
    env = os.environ.copy()
    env["OPENAI_BASE_URL"] = f"{mock.base_url}/v1"
    env["OPENAI_API_KEY"] = "mock-key"
    env["OPENAI_MODEL"] = model

    try:
        proc = await asyncio.create_subprocess_exec(
            "qwen",
            "--acp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=16 * 1024 * 1024,
        )
    except OSError as exc:
        report.add(
            CheckResult(
                "D. ambient-settings canary (informational)",
                ok=True,
                required=False,
                detail=f"could not spawn for canary check ({exc}); skipped",
            )
        )
        return

    async def rpc(msg_id: int, method: str, params: dict) -> dict:
        req = (
            json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}) + "\n"
        )
        proc.stdin.write(req.encode())
        await proc.stdin.drain()
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=20)
        return json.loads(line)

    try:
        await rpc(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        new_resp = await rpc(2, "session/new", {"cwd": str(REPO_ROOT), "mcpServers": []})
        current_model = new_resp.get("result", {}).get("models", {}).get("currentModelId", "")
        hijacked = model not in current_model
        report.add(
            CheckResult(
                "D. ambient-settings canary (informational)",
                ok=not hijacked,
                required=False,
                detail=(
                    f"unisolated session/new picked model {current_model!r} instead of "
                    f"{model!r} — this machine's ~/.qwen/settings.json WOULD hijack an "
                    "unisolated gateway request right now. The isolation fix is "
                    "load-bearing here."
                    if hijacked
                    else "unisolated request honored the env-var model; no ambient "
                    "hijack on this machine right now."
                ),
            )
        )
    except (asyncio.TimeoutError, json.JSONDecodeError) as exc:
        report.add(
            CheckResult(
                "D. ambient-settings canary (informational)",
                ok=True,
                required=False,
                detail=f"canary check inconclusive: {exc}",
            )
        )
    finally:
        with contextlib.suppress(Exception):
            # Explicitly close stdin's transport via the public StreamWriter
            # API before the process/transport objects go out of scope --
            # otherwise their __del__ can fire after asyncio.run() has
            # already closed the loop and print a spurious (harmless)
            # "Event loop is closed" traceback.
            proc.stdin.close()
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)


def _load_golden() -> dict:
    if GOLDEN_PATH.is_file():
        return json.loads(GOLDEN_PATH.read_text())
    return {}


def _write_golden(version: str | None, report: Report) -> None:
    GOLDEN_PATH.write_text(
        json.dumps(
            {
                "qwen_version": version,
                "captured_at_checks": {c.name: c.ok for c in report.checks},
                "note": "Reference snapshot from a known-good qwen_doctor.py run. "
                "Regenerate with --capture after confirming a fresh run is genuinely healthy.",
            },
            indent=2,
        )
        + "\n"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capture",
        action="store_true",
        help="After a healthy run, refresh scripts/qwen_doctor_golden.json with this "
        "run's version info.",
    )
    args = parser.parse_args()

    golden = _load_golden()
    report = Report()

    print("=== qwen_doctor: isolated health check (no pytest, no ambient config touched) ===\n")

    version = _check_qwen_on_path(report, golden)
    if version is None:
        _print_summary(report)
        return 1

    await _check_raw_acp_handshake(report)

    mock = _MockServer(port=41799)
    model = "qwen-doctor-mock-model"
    try:
        mock.start()
        mock.configure(model, CANNED_TEXT)
        await _check_isolated_gateway_routing(report, mock, model)
        await _check_ambient_settings_canary(report, mock, model)
    finally:
        mock.stop()

    _print_summary(report)

    if args.capture and report.healthy:
        _write_golden(version, report)
        print(f"\nGolden reference updated: {GOLDEN_PATH}")

    return 0 if report.healthy else 1


def _print_summary(report: Report) -> None:
    print("\n=== Summary ===")
    if report.healthy:
        print("HEALTHY — all required checks passed.")
    else:
        failed = [c.name for c in report.checks if not c.ok and c.required]
        print(f"UNHEALTHY — failed: {', '.join(failed)}")
        print("See heal hints above. docs/QWEN_FOLLOWUPS.md has the full incident writeup.")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
