"""Provider-neutral usage limits read by asking each CLI for its own usage.

Claude answers the ``get_usage`` control request and Codex answers
``account/rateLimits/read``. Each CLI uses its own login, so Omnigent never
reads a token or the Keychain. Readings are reduced to an allowlist of
windows before they leave this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import shutil
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CLAUDE_PROBE_SECONDS = 30.0
CODEX_PROBE_SECONDS = 15.0
CACHE_SECONDS = 300.0
FAILURE_BACKOFF_SECONDS = 300.0
# After an answer the CLI may exit on its own; then its process group is killed.
_EXIT_GRACE_SECONDS = 5.0
_REAP_SECONDS = 1.0
MODEL_WINDOWS_MAX = 8
LABEL_MAX_CHARS = 64
# Initialize responses list commands and models; allow lines far above 64 KiB.
_STREAM_LIMIT_BYTES = 16 * 1024 * 1024
_SESSION_MINUTES = 5 * 60
_WEEK_MINUTES = 7 * 24 * 60
_MONTH_MINUTES = 30 * 24 * 60

# No tools, MCP servers, hooks, settings files, slash commands or saved session.
CLAUDE_PROBE_ARGS = (
    "--output-format",
    "stream-json",
    "--input-format",
    "stream-json",
    "--verbose",
    "--tools",
    "",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--settings",
    '{"disableAllHooks":true}',
    "--setting-sources",
    "",
    "--disable-slash-commands",
    "--no-session-persistence",
)
CODEX_PROBE_ARGS = ("-s", "read-only", "-a", "never", "app-server")

Provider = Literal["claude", "codex"]
WindowKind = Literal["session", "weekly", "weekly_model", "monthly"]


class UsageWindow(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: WindowKind
    label: str = Field(min_length=1, max_length=LABEL_MAX_CHARS)
    percent_used: float = Field(ge=0, le=100, allow_inf_nan=False)
    resets_at: datetime | None = None
    window_minutes: int | None = Field(default=None, gt=0)


class AccountUsage(BaseModel):
    """One account's usage snapshot; never carries identity or credentials.

    ``stale`` is the last successful reading after a later probe failed.
    ``observed_at`` is when that reading was taken.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    provider: Provider
    account: str
    status: Literal["available", "stale", "unavailable"] = "unavailable"
    windows: list[UsageWindow] = Field(default_factory=list)
    observed_at: datetime | None = None


class ProbeError(Exception):
    """The CLI could not report usage; the message never carries CLI output."""


def _percent(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return min(100.0, max(0.0, float(value)))


def _iso_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _epoch_time(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    label = value.strip()
    if not label or len(label) > LABEL_MAX_CHARS or not label.isprintable():
        return None
    return label


def _snapshot(
    provider: Provider, account: str, windows: list[UsageWindow], observed_at: datetime
) -> AccountUsage:
    if not windows:
        return AccountUsage(provider=provider, account=account)
    return AccountUsage(
        provider=provider,
        account=account,
        status="available",
        windows=windows,
        observed_at=observed_at,
    )


def normalize_claude_usage(account: str, response: object, observed_at: datetime) -> AccountUsage:
    """Reduce a Claude ``get_usage`` response; percentages are already 0-100."""
    if not isinstance(response, dict) or response.get("rate_limits_available") is not True:
        return AccountUsage(provider="claude", account=account)
    limits = response.get("rate_limits")
    if not isinstance(limits, dict):
        return AccountUsage(provider="claude", account=account)
    windows: list[UsageWindow] = []
    for key, kind, label, minutes in (
        ("five_hour", "session", "5-hour", _SESSION_MINUTES),
        ("seven_day", "weekly", "Weekly", _WEEK_MINUTES),
    ):
        window = limits.get(key)
        used = _percent(window.get("utilization")) if isinstance(window, dict) else None
        if isinstance(window, dict) and used is not None:
            windows.append(
                UsageWindow(
                    kind=kind,
                    label=label,
                    percent_used=used,
                    resets_at=_iso_time(window.get("resets_at")),
                    window_minutes=minutes,
                )
            )
    scoped = limits.get("model_scoped")
    for entry in scoped if isinstance(scoped, list) else []:
        if sum(w.kind == "weekly_model" for w in windows) >= MODEL_WINDOWS_MAX:
            break
        if not isinstance(entry, dict):
            continue
        label = _label(entry.get("display_name"))
        used = _percent(entry.get("utilization"))
        if label is not None and used is not None:
            windows.append(
                UsageWindow(
                    kind="weekly_model",
                    label=label,
                    percent_used=used,
                    resets_at=_iso_time(entry.get("resets_at")),
                    window_minutes=_WEEK_MINUTES,
                )
            )
    return _snapshot("claude", account, windows, observed_at)


def _codex_kind(minutes: int) -> WindowKind:
    if minutes >= _MONTH_MINUTES:
        return "monthly"
    return "weekly" if minutes >= _WEEK_MINUTES else "session"


def _codex_label(kind: WindowKind, minutes: int) -> str:
    if kind == "session":
        return "5-hour" if minutes == _SESSION_MINUTES else "Session"
    return "Weekly" if kind == "weekly" else "Monthly"


def normalize_codex_rate_limits(
    account: str, result: object, observed_at: datetime
) -> AccountUsage:
    """Reduce an ``account/rateLimits/read`` result.

    ``primary`` and ``secondary`` are positions, not durations: some plans
    report a weekly ``primary``, so windows are classified by their length.
    """
    snapshot = None
    if isinstance(result, dict):
        by_limit = result.get("rateLimitsByLimitId")
        snapshot = by_limit.get("codex") if isinstance(by_limit, dict) else None
        if snapshot is None:
            snapshot = result.get("rateLimits")
    # Model-specific buckets are not the account's main allowance.
    if not isinstance(snapshot, dict) or snapshot.get("limitId") not in (None, "codex"):
        return AccountUsage(provider="codex", account=account)
    windows: list[UsageWindow] = []
    for key in ("primary", "secondary"):
        window = snapshot.get(key)
        if not isinstance(window, dict) or (used := _percent(window.get("usedPercent"))) is None:
            continue
        minutes = window.get("windowDurationMins")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            continue
        kind = _codex_kind(minutes)
        windows.append(
            UsageWindow(
                kind=kind,
                label=_codex_label(kind, minutes),
                percent_used=used,
                resets_at=_epoch_time(window.get("resetsAt")),
                window_minutes=minutes,
            )
        )
    individual = snapshot.get("individualLimit")
    remaining = (
        _percent(individual.get("remainingPercent")) if isinstance(individual, dict) else None
    )
    if isinstance(individual, dict) and remaining is not None:
        windows.append(
            UsageWindow(
                kind="monthly",
                label="Monthly credit limit",
                percent_used=100.0 - remaining,
                resets_at=_epoch_time(individual.get("resetsAt")),
            )
        )
    return _snapshot("codex", account, windows, observed_at)


async def _read_message(
    stdout: asyncio.StreamReader, matches: Callable[[dict[str, object]], bool]
) -> dict[str, object]:
    while True:
        line = await stdout.readline()
        if not line:
            raise ProbeError("CLI exited before answering")
        with contextlib.suppress(ValueError):
            message = json.loads(line)
            if isinstance(message, dict) and matches(message):
                return message


async def _write_line(proc: asyncio.subprocess.Process, message: dict[str, object]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(message).encode() + b"\n")
    await proc.stdin.drain()


async def _stop(proc: asyncio.subprocess.Process, *, graceful: bool, deadline: float) -> None:
    """After an answer, let the CLI exit on closed stdin; then kill its process group.

    Helpers the CLI started can hold stdout open after it exits, so no wait
    here depends on the pipes closing.
    """
    if proc.stdin is not None and not proc.stdin.is_closing():
        proc.stdin.close()
    if graceful:
        # ``returncode`` is set when the CLI exits, even while helpers hold its pipes.
        loop = asyncio.get_running_loop()
        exit_deadline = min(deadline, loop.time() + _EXIT_GRACE_SECONDS)
        while proc.returncode is None:
            remaining = exit_deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.05, remaining))
    # The probe leads its own session, so its pid is also its process group id.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=_REAP_SECONDS)


async def _spawn(command: str, args: tuple[str, ...], env: Mapping[str, str], cwd: str):
    return await asyncio.create_subprocess_exec(
        command,
        *args,
        cwd=cwd,
        env=dict(env),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=_STREAM_LIMIT_BYTES,
        start_new_session=True,
    )


async def _claude_control(
    proc: asyncio.subprocess.Process, request_id: str, request: dict[str, object]
) -> object:
    await _write_line(
        proc, {"type": "control_request", "request_id": request_id, "request": request}
    )
    assert proc.stdout is not None
    message = await _read_message(
        proc.stdout,
        lambda m: (
            m.get("type") == "control_response"
            and isinstance(m.get("response"), dict)
            and m["response"].get("request_id") == request_id
        ),
    )
    response = message["response"]
    assert isinstance(response, dict)
    if response.get("subtype") != "success":
        raise ProbeError(f"{request['subtype']} failed")
    return response.get("response")


def claude_probe_environment(config_dir: str | None) -> dict[str, str]:
    """Return the environment that selects one Claude login and no extras."""
    env = os.environ.copy()
    if config_dir is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = config_dir
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        env.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
    # With this kill-switch the CLI only replays its own usage cache, which can be missing or old.
    env.pop("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", None)
    env.update(
        ENABLE_CLAUDEAI_MCP_SERVERS="false",
        CLAUDE_CODE_AUTO_CONNECT_IDE="0",
        CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL="1",
    )
    return env


async def probe_claude(account: str, config_dir: str | None, command: str | None) -> AccountUsage:
    """Ask the Claude CLI for one login's usage without sending a prompt."""
    if command is None:
        raise ProbeError("claude not found")
    deadline = asyncio.get_running_loop().time() + CLAUDE_PROBE_SECONDS
    with tempfile.TemporaryDirectory(prefix="omnigent-usage-") as cwd:
        proc = await _spawn(command, CLAUDE_PROBE_ARGS, claude_probe_environment(config_dir), cwd)
        answered = False
        try:
            async with asyncio.timeout_at(deadline):
                await _claude_control(proc, "usage_init", {"subtype": "initialize", "hooks": None})
                observed_at = datetime.now(UTC)
                response = await _claude_control(
                    proc, "usage_read", {"subtype": "get_usage", "skip_behaviors": True}
                )
            answered = True
        finally:
            await _stop(proc, graceful=answered, deadline=deadline)
    return normalize_claude_usage(account, response, observed_at)


async def _codex_request(
    proc: asyncio.subprocess.Process, request_id: int, method: str, params: object
) -> object:
    await _write_line(proc, {"id": request_id, "method": method, "params": params})
    assert proc.stdout is not None
    message = await _read_message(
        proc.stdout, lambda m: m.get("id") == request_id and "method" not in m
    )
    if "error" in message:
        raise ProbeError(f"{method} failed")
    return message.get("result")


async def probe_codex(account: str, codex_home: str | None, command: str | None) -> AccountUsage:
    """Ask ``codex app-server`` for one home's rate limits."""
    if command is None:
        raise ProbeError("codex not found")
    deadline = asyncio.get_running_loop().time() + CODEX_PROBE_SECONDS
    env = os.environ.copy()
    if codex_home is not None:
        env["CODEX_HOME"] = codex_home
    with tempfile.TemporaryDirectory(prefix="omnigent-usage-") as cwd:
        proc = await _spawn(command, CODEX_PROBE_ARGS, env, cwd)
        answered = False
        try:
            async with asyncio.timeout_at(deadline):
                await _codex_request(
                    proc,
                    1,
                    "initialize",
                    {"clientInfo": {"name": "omnigent", "version": "0.1"}},
                )
                await _write_line(proc, {"method": "initialized"})
                observed_at = datetime.now(UTC)
                result = await _codex_request(proc, 2, "account/rateLimits/read", None)
            answered = True
        finally:
            await _stop(proc, graceful=answered, deadline=deadline)
    return normalize_codex_rate_limits(account, result, observed_at)


@dataclass
class _Slot:
    reading: AccountUsage | None = None
    last_good: AccountUsage | None = None
    checked: float | None = None
    task: asyncio.Task[AccountUsage] | None = None


class UsageCache:
    """Reuse each account's reading for five minutes, and back off after failure.

    Concurrent readers of one account share a single in-flight probe. A failed
    probe serves the last good reading as ``stale`` until the back-off ends.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._slots: dict[tuple[str, ...], _Slot] = {}

    async def read(
        self,
        key: tuple[str, ...],
        unavailable: AccountUsage,
        probe: Callable[[], Awaitable[AccountUsage]],
    ) -> AccountUsage:
        slot = self._slots.setdefault(key, _Slot())
        if slot.reading is not None and slot.checked is not None:
            hold = CACHE_SECONDS if slot.reading.status == "available" else FAILURE_BACKOFF_SECONDS
            if self._clock() - slot.checked < hold:
                return slot.reading
        if slot.task is None:
            slot.task = asyncio.ensure_future(self._refresh(slot, unavailable, probe))
        # A caller that gives up must not cancel the probe other callers share.
        return await asyncio.shield(slot.task)

    async def _refresh(
        self,
        slot: _Slot,
        unavailable: AccountUsage,
        probe: Callable[[], Awaitable[AccountUsage]],
    ) -> AccountUsage:
        try:
            reading = await probe()
        # Malformed answers raise KeyError/TypeError; none may break the host reply.
        except (OSError, TimeoutError, ProbeError, ValueError, KeyError, TypeError):
            reading = unavailable
        finally:
            slot.task = None
        if reading.status == "available":
            slot.last_good = reading
        elif slot.last_good is not None:
            reading = slot.last_good.model_copy(update={"status": "stale"})
        slot.reading = reading
        slot.checked = self._clock()
        return reading


_CACHE = UsageCache()


async def read_usage_limits(
    providers: tuple[Provider, ...] = ("claude", "codex"), cache: UsageCache | None = None
) -> list[AccountUsage]:
    """Read this host's Claude and Codex logins in parallel."""
    cache = cache or _CACHE
    reads: list[Awaitable[AccountUsage]] = []
    if "claude" in providers:
        claude = shutil.which("claude")
        folder = os.environ.get("CLAUDE_CONFIG_DIR") or None
        reads.append(
            cache.read(
                ("claude", "default", folder or ""),
                AccountUsage(provider="claude", account="default"),
                lambda: probe_claude("default", folder, claude),
            )
        )
    if "codex" in providers:
        codex = shutil.which("codex")
        home = os.environ.get("CODEX_HOME") or None
        reads.append(
            cache.read(
                ("codex", "default", home or ""),
                AccountUsage(provider="codex", account="default"),
                lambda: probe_codex("default", home, codex),
            )
        )
    return list(await asyncio.gather(*reads))


def main() -> None:
    """Print this host's sanitized readings, for checking accounts by hand."""
    readings = asyncio.run(read_usage_limits())
    print(json.dumps([reading.model_dump(mode="json") for reading in readings], indent=2))


if __name__ == "__main__":
    main()
