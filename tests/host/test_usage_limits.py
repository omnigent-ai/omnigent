"""Native usage probes ask each CLI for usage and publish only an allowlist."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil
import pytest

from omnigent.host import usage_limits
from omnigent.host.usage_limits import AccountUsage, UsageCache, UsageWindow

FIXTURES = Path(__file__).parent / "fixtures" / "usage_limits"
CLAUDE_USAGE = json.loads((FIXTURES / "claude_get_usage.json").read_text())
CODEX_LIMITS = json.loads((FIXTURES / "codex_rate_limits.json").read_text())
OBSERVED = datetime(2026, 11, 1, 12, tzinfo=UTC)


def _windows(usage: AccountUsage) -> list[tuple[str, str, float, int | None]]:
    return [(w.kind, w.label, w.percent_used, w.window_minutes) for w in usage.windows]


def test_claude_usage_keeps_account_and_model_windows_only():
    usage = usage_limits.normalize_claude_usage("work", CLAUDE_USAGE, OBSERVED)
    assert usage.status == "available"
    assert usage.observed_at == OBSERVED
    assert _windows(usage) == [
        ("session", "5-hour", 16.0, 300),
        ("weekly", "Weekly", 42.0, 10080),
        ("weekly_model", "Fable", 3.0, 10080),
    ]
    assert usage.windows[0].resets_at == datetime(2026, 11, 1, 15, tzinfo=UTC)
    wire = usage.model_dump_json()
    for private in ("max", "synthetic", "USD", "claude_code", "Claude Code"):
        assert private not in wire


@pytest.mark.parametrize(
    "response",
    [
        {**CLAUDE_USAGE, "rate_limits_available": False},
        {"rate_limits_available": True, "rate_limits": None},
        {"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": True}}},
        "not an object",
    ],
)
def test_claude_usage_without_windows_is_unavailable(response):
    usage = usage_limits.normalize_claude_usage("work", response, OBSERVED)
    assert (usage.status, usage.windows, usage.observed_at) == ("unavailable", [], None)


def test_claude_usage_tolerates_odd_values():
    limits = {
        "five_hour": {"utilization": 130, "resets_at": None},
        "seven_day": {"utilization": float("nan"), "resets_at": "2026-11-05T09:00:00Z"},
        "model_scoped": [
            {"display_name": "\x1b[31m", "utilization": 1, "resets_at": None},
            *[{"display_name": f"M{i}", "utilization": i, "resets_at": None} for i in range(10)],
        ],
    }
    usage = usage_limits.normalize_claude_usage(
        "work", {"rate_limits_available": True, "rate_limits": limits}, OBSERVED
    )
    assert _windows(usage)[0] == ("session", "5-hour", 100.0, 300)
    assert usage.windows[0].resets_at is None
    assert [w.label for w in usage.windows[1:]] == [f"M{i}" for i in range(8)]


def test_codex_limits_classify_by_duration_and_keep_main_bucket():
    usage = usage_limits.normalize_codex_rate_limits("default", CODEX_LIMITS, OBSERVED)
    assert _windows(usage) == [("session", "5-hour", 12.0, 300), ("weekly", "Weekly", 47.0, 10080)]
    assert usage.windows[1].resets_at == datetime.fromtimestamp(1793988000, UTC)
    assert "private-account-id" not in usage.model_dump_json()


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        # Some plans report the weekly window as primary with no secondary.
        (
            {"primary": {"usedPercent": 5, "windowDurationMins": 10080}, "secondary": None},
            [("weekly", "Weekly", 5.0, 10080)],
        ),
        (
            {"planType": "free", "primary": {"usedPercent": 80, "resetsAt": None}},
            [],
        ),
        (
            {"individualLimit": {"remainingPercent": 99, "resetsAt": 1793988000}},
            [("monthly", "Monthly credit limit", 1.0, None)],
        ),
        ({"limitId": "codex_spark", "primary": {"usedPercent": 5}}, []),
    ],
)
def test_codex_limit_shapes(snapshot, expected):
    usage = usage_limits.normalize_codex_rate_limits("default", {"rateLimits": snapshot}, OBSERVED)
    assert _windows(usage) == expected
    assert usage.status == ("available" if expected else "unavailable")


@pytest.fixture
def probe_host(tmp_path: Path, monkeypatch):
    """Isolated host with fake ``claude`` and ``codex`` CLIs on PATH."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.delenv("OMNIGENT_CONFIG_HOME", raising=False)
    monkeypatch.delenv("OMNIGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "private-token")
    # An inherited kill-switch would make the CLI replay its cache instead of fetching.
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    log = tmp_path / "calls.log"
    claude = tmp_path / "claude"
    claude.write_text(f"""#!{sys.executable}
import json, os, subprocess, sys, time
assert tuple(sys.argv[1:]) == {usage_limits.CLAUDE_PROBE_ARGS!r}, sys.argv
assert os.environ["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in os.environ
helper_pid_file = os.environ.get("FAKE_HELPER_PID_FILE")
if helper_pid_file:
    # A launcher-style helper inherits stdout and outlives the CLI.
    helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(helper_pid_file, "w") as pid_file:
        pid_file.write(str(helper.pid))
folder = os.environ.get("CLAUDE_CONFIG_DIR")
if folder:
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
with open({str(log)!r}, "a") as log:
    log.write(f"claude {{folder or 'default'}}\\n")
mode = os.environ.get("FAKE_USAGE_MODE")
if mode == "late":
    with open(os.environ["FAKE_CLI_PID_FILE"], "w") as pid_file:
        pid_file.write(str(os.getpid()))
if mode == "exit":
    sys.exit(1)
usage = json.load(open({str(FIXTURES / "claude_get_usage.json")!r}))
if folder:
    usage["rate_limits"]["five_hour"]["utilization"] = 70
for line in sys.stdin:
    message = json.loads(line)
    # A prompt would spend quota: only control requests are allowed.
    assert message["type"] == "control_request", message
    request = message["request"]
    print(json.dumps({{"type": "system", "subtype": "noise"}}), flush=True)
    if request["subtype"] == "initialize":
        body = {{"account": {{"email": "private@example.com"}}}}
    elif mode == "hang":
        time.sleep(30)
    elif mode == "error":
        print(json.dumps({{"type": "control_response", "response": {{
            "subtype": "error", "request_id": message["request_id"],
            "error": "PRIVATE ERROR"}}}}), flush=True)
        continue
    else:
        assert request == {{"subtype": "get_usage", "skip_behaviors": True}}
        body = usage
        if mode == "late":
            time.sleep(1.5)
    print(json.dumps({{"type": "control_response", "response": {{
        "subtype": "success", "request_id": message["request_id"], "response": body}}}}),
        flush=True)
    if mode == "late" and request["subtype"] == "get_usage":
        time.sleep(60)
""")
    codex = tmp_path / "codex"
    codex.write_text(f"""#!{sys.executable}
import json, os, sys
assert tuple(sys.argv[1:]) == {usage_limits.CODEX_PROBE_ARGS!r}, sys.argv
with open({str(log)!r}, "a") as log:
    log.write(f"codex {{os.environ.get('CODEX_HOME', 'default')}}\\n")
initialized = False
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialized":
        initialized = True
        continue
    if message["method"] == "initialize":
        result = {{"userAgent": "codex/0.166.0"}}
    else:
        assert initialized and message["method"] == "account/rateLimits/read"
        result = json.load(open({str(FIXTURES / "codex_rate_limits.json")!r}))
    print(json.dumps({{"method": "account/updated", "params": {{}}}}), flush=True)
    print(json.dumps({{"id": message["id"], "result": result}}), flush=True)
""")
    claude.chmod(0o700)
    codex.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    return log


@pytest.mark.parametrize("folder", [None, "work"])
def test_probes_read_each_login_without_prompting(probe_host, monkeypatch, folder):
    log = probe_host
    if folder:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(log.parent / folder))
    monkeypatch.setenv("CODEX_HOME", "/codex-home")
    readings = asyncio.run(usage_limits.read_usage_limits(cache=UsageCache()))
    assert [(r.provider, r.account, r.status) for r in readings] == [
        ("claude", "default", "available"),
        ("codex", "default", "available"),
    ]
    assert [r.windows[0].percent_used for r in readings] == [70 if folder else 16, 12]
    claude_call = f"claude {log.parent / folder}" if folder else "claude default"
    assert sorted(log.read_text().splitlines()) == [claude_call, "codex /codex-home"]
    wire = json.dumps([r.model_dump(mode="json") for r in readings])
    assert "private" not in wire and str(log.parent) not in wire


@pytest.mark.parametrize("mode", ["exit", "hang", "error"])
def test_probe_failures_are_unavailable(probe_host, monkeypatch, mode):
    monkeypatch.setenv("FAKE_USAGE_MODE", mode)
    monkeypatch.setattr(usage_limits, "CLAUDE_PROBE_SECONDS", 1.0)
    readings = asyncio.run(usage_limits.read_usage_limits(("claude",), cache=UsageCache()))
    assert [(r.account, r.status, r.windows) for r in readings] == [
        ("default", "unavailable", []),
    ]


def test_claude_probe_lets_the_cli_fetch_usage(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    for folder in (None, "/work"):
        env = usage_limits.claude_probe_environment(folder)
        assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in env
        assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"


def _process_stopped(pid: int, wait_seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            # A killed orphan can remain a zombie until its new parent reaps it.
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.posix_only
def test_process_stopped_recognizes_an_unreaped_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert not _process_stopped(child.pid, wait_seconds=0.01)
        child.kill()
        deadline = time.monotonic() + 3.0
        while psutil.Process(child.pid).status() != psutil.STATUS_ZOMBIE:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert _process_stopped(child.pid, wait_seconds=0.01)
    finally:
        child.kill()
        child.wait()


@pytest.mark.parametrize(("mode", "status"), [("hang", None), (None, "available")])
async def test_probe_stops_helpers_that_hold_its_output_open(
    probe_host, monkeypatch, tmp_path, mode, status
):
    pid_file = tmp_path / "helper.pid"
    monkeypatch.setenv("FAKE_HELPER_PID_FILE", str(pid_file))
    if mode is not None:
        monkeypatch.setenv("FAKE_USAGE_MODE", mode)
    monkeypatch.setattr(usage_limits, "CLAUDE_PROBE_SECONDS", 2.0)
    started = time.monotonic()
    try:
        reading = await usage_limits.probe_claude("work", None, str(tmp_path / "claude"))
    except TimeoutError:
        reading = None
    elapsed = time.monotonic() - started
    assert (reading.status if reading else None) == status
    # Both paths finish well before the helper's own 60 s lifetime.
    assert elapsed < usage_limits.CLAUDE_PROBE_SECONDS + usage_limits._REAP_SECONDS + 1.5
    assert _process_stopped(int(pid_file.read_text()))


async def test_late_answer_shares_the_probe_deadline_with_shutdown(
    probe_host, monkeypatch, tmp_path
):
    pid_file = tmp_path / "cli.pid"
    monkeypatch.setenv("FAKE_USAGE_MODE", "late")
    monkeypatch.setenv("FAKE_CLI_PID_FILE", str(pid_file))
    monkeypatch.setattr(usage_limits, "CLAUDE_PROBE_SECONDS", 5.0)
    started = time.monotonic()
    reading = await usage_limits.probe_claude("work", None, str(tmp_path / "claude"))
    elapsed = time.monotonic() - started
    assert reading.status == "available"
    assert elapsed < usage_limits.CLAUDE_PROBE_SECONDS + usage_limits._REAP_SECONDS
    assert _process_stopped(int(pid_file.read_text()))


def test_missing_cli_is_unavailable(probe_host, monkeypatch, tmp_path):
    (tmp_path / "claude").unlink()
    (tmp_path / "codex").unlink()
    readings = asyncio.run(usage_limits.read_usage_limits(cache=UsageCache()))
    assert [(r.provider, r.status) for r in readings] == [
        ("claude", "unavailable"),
        ("codex", "unavailable"),
    ]


def _reading(percent: float) -> AccountUsage:
    window = UsageWindow(kind="session", label="5-hour", percent_used=percent)
    return AccountUsage(
        provider="claude", account="a", status="available", windows=[window], observed_at=OBSERVED
    )


def test_cache_shares_probes_reuses_readings_and_backs_off():
    now = [0.0]
    cache = UsageCache(clock=lambda: now[0])
    unavailable = AccountUsage(provider="claude", account="a")
    results: list[AccountUsage | Exception] = [_reading(10), TimeoutError(), _reading(30)]
    calls: list[int] = []

    async def probe() -> AccountUsage:
        calls.append(1)
        await asyncio.sleep(0.01)
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def read() -> AccountUsage:
        return await cache.read(("claude", "a"), unavailable, probe)

    async def scenario() -> list[tuple[str, float | None]]:
        seen = []
        # Concurrent readers share one probe.
        first = await asyncio.gather(read(), read(), read())
        assert len(calls) == 1 and first[0] is first[2]
        seen.append(first[0])
        now[0] = 299
        seen.append(await read())  # still cached
        now[0] = 300
        seen.append(await read())  # probe fails: last reading, marked stale
        now[0] = 599
        seen.append(await read())  # backing off: no new probe
        now[0] = 600
        seen.append(await read())  # retried after the back-off
        return [(r.status, r.windows[0].percent_used) for r in seen]

    assert asyncio.run(scenario()) == [
        ("available", 10),
        ("available", 10),
        ("stale", 10),
        ("stale", 10),
        ("available", 30),
    ]
    assert len(calls) == 3


def test_first_failure_without_reading_is_unavailable():
    cache = UsageCache(clock=lambda: 0.0)
    unavailable = AccountUsage(provider="codex", account="default")

    async def probe() -> AccountUsage:
        raise usage_limits.ProbeError("codex not found")

    assert asyncio.run(cache.read(("codex",), unavailable, probe)) is unavailable


@pytest.mark.parametrize("minutes", [None, 0, -1, True, 300.0, "10080"])
@pytest.mark.parametrize("position", ["primary", "secondary"])
def test_codex_omits_windows_without_positive_integer_duration(minutes, position):
    reading = usage_limits.normalize_codex_rate_limits(
        "default",
        {
            "rateLimits": {
                "planType": "pro",
                position: {
                    "usedPercent": 40,
                    "windowDurationMins": minutes,
                },
            }
        },
        OBSERVED,
    )
    assert (reading.status, reading.windows) == ("unavailable", [])


@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_missing_cli_preserves_stale_reading_and_backoff(probe_host, tmp_path, provider):
    now = [0.0]
    cache = UsageCache(clock=lambda: now[0])
    first = await usage_limits.read_usage_limits((provider,), cache=cache)
    command = tmp_path / provider
    command.rename(tmp_path / (provider + "-hidden"))
    now[0] = 300
    stale = await usage_limits.read_usage_limits((provider,), cache=cache)
    assert all(r.status == "stale" for r in stale)
    assert [r.windows for r in stale] == [r.windows for r in first]
    (tmp_path / (provider + "-hidden")).rename(command)
    now[0] = 599
    assert await usage_limits.read_usage_limits((provider,), cache=cache) == stale
    now[0] = 600
    assert all(
        r.status == "available"
        for r in await usage_limits.read_usage_limits((provider,), cache=cache)
    )
