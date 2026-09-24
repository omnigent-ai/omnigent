"""Retain execution evidence; records describe observations, not verified claims."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .doctor import launch_observations
from .runtime import write_json

MAX_EVENT = 256 * 1024
MAX_OUTPUT = 8 * 1024 * 1024


def clean(value):
    """Omit credentials from structured observations; bundle scanning still applies."""
    if isinstance(value, dict):
        if re.search(
            r"authorization|cookie|password|secret|api.?key|token",
            str(value.get("name", "")),
            re.I,
        ):
            value = {**value, "value": "[redacted]"} if "value" in value else value
        return {
            k: "[redacted]"
            if re.search(r"authorization|cookie|password|secret|api.?key|token", k, re.I)
            else clean(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    if isinstance(value, str):
        for name, secret in os.environ.items():
            if len(secret) >= 8 and re.search(r"TOKEN|SECRET|PASSWORD|API_KEY", name):
                value = value.replace(secret, "[redacted]")
        value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[redacted]", value)
    return value


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class Journal:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / f"events-{os.getpid()}-{uuid.uuid4().hex}.jsonl"
        self.lock = threading.Lock()

    def emit(self, kind: str, **data) -> None:
        event = {"time_ns": time.time_ns(), "kind": kind, **clean(data)}
        encoded = json.dumps(event, ensure_ascii=True)
        if len(encoded) > MAX_EVENT:
            event = {
                "time_ns": event["time_ns"],
                "kind": kind,
                "truncated": True,
                "original_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                "preview": encoded[:MAX_EVENT],
            }
        with self.lock, self.path.open("a") as stream:
            stream.write(json.dumps(event) + "\n")


def sanitize_trace(path: Path) -> None:
    """Redact text resources and keep ZIP members visible to the bundle byte scan."""
    temporary = path.with_suffix(".tmp")
    try:
        with (
            zipfile.ZipFile(path) as source,
            zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as target,
        ):
            for member in source.infolist():
                data = source.read(member)
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError:
                    pass
                else:
                    lines = []
                    for line in content.splitlines(keepends=True):
                        try:
                            lines.append(json.dumps(clean(json.loads(line))) + "\n")
                        except ValueError:
                            lines.append(clean(line))
                    data = "".join(lines).encode()
                target.writestr(member.filename, data)
        temporary.replace(path)
    except BaseException:
        path.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        raise


def inventory(directory: Path) -> list[dict]:
    result = []
    for parent, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(parent) / d).is_symlink()]
        for name in names:
            path = Path(parent) / name
            if path.is_file() and not path.is_symlink() and path.name != "attempt.json":
                result.append(
                    {
                        "path": str(path.relative_to(directory)),
                        "bytes": path.stat().st_size,
                        "sha256": digest_file(path),
                    }
                )
    return sorted(result, key=lambda row: row["path"])


def run(output: Path, command: list[str], env: dict[str, str], *, prepare=None) -> int:
    """Record every opted-in command, including failed and interrupted attempts."""
    context = json.loads((output / "execution-context.json").read_text())
    attempt_id = uuid.uuid4().hex
    directory = output / "execution" / attempt_id
    journal = Journal(directory)
    root = Path.cwd()
    record = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "context": context,
        "started_at_ns": time.time_ns(),
        "status": "incomplete",
        "command": clean(command),
        "cwd": str(root),
        "checkout": launch_observations(root),
        "python": sys.version,
        "producer": "repro_env_exec",
        "limitations": [
            "Agent-workspace observations, not an independent verifier.",
            "Only wrapped commands and supported pytest/browser paths are instrumented.",
            "No observed event does not prove an action was absent.",
        ],
    }
    write_json(directory / "attempt.json", record)
    plan = root / ".omnigent/reproduction-plan.json"
    record["working_plan_sha256"] = digest_file(plan) if plan.is_file() else None
    launch = output / "launch-observations.json"
    record["runtime_launch"] = json.loads(launch.read_text()) if launch.is_file() else None
    changed = subprocess.run(
        ["git", "ls-files", "-z", "--modified", "--others", "--exclude-standard"],
        capture_output=True,
        cwd=root,
    )
    record["changed_files"] = [
        {"path": name, "sha256": digest_file(root / name)}
        for name in changed.stdout.decode(errors="replace").split("\0")
        if name and (root / name).is_file() and not (root / name).is_symlink()
    ]
    diff = subprocess.run(["git", "diff", "--binary", "HEAD"], capture_output=True, cwd=root)
    record["tracked_diff_sha256"] = (
        hashlib.sha256(diff.stdout).hexdigest() if not diff.returncode else None
    )
    record["command_files"] = [
        {"path": arg, "sha256": digest_file(root / arg)}
        for arg in command
        if not arg.startswith("-") and (root / arg).is_file() and not (root / arg).is_symlink()
    ]
    write_json(directory / "attempt.json", record)
    stack = contextlib.ExitStack()
    process = None
    interrupted = False
    old_signals = {}
    threads = []

    def forward(signum, _frame):
        nonlocal interrupted
        interrupted = True
        journal.emit("signal", number=signum)
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signum)

    def copy(stream, destination, name):
        total = 0
        with (directory / name).open("w") as saved:
            for line in iter(lambda: stream.readline(65536), b""):
                destination.write(line.decode(errors="replace"))
                destination.flush()
                text = clean(line.decode(errors="replace"))
                if total < MAX_OUTPUT:
                    saved.write(text[: MAX_OUTPUT - total])
                    saved.flush()
                total += len(text)
        if total > MAX_OUTPUT:
            journal.emit("output_truncated", stream=name, original_characters=total)

    try:
        if prepare is not None:
            env = stack.enter_context(prepare())
        child_env = {**env, "OMNIGENT_REPRO_ATTEMPT_DIR": str(directory.resolve())}
        plugins = [p for p in child_env.get("PYTEST_PLUGINS", "").split(",") if p]
        if "dev.repro_env.pytest_evidence" not in plugins:
            plugins.append("dev.repro_env.pytest_evidence")
        child_env["PYTEST_PLUGINS"] = ",".join(plugins)
        # Console-script pytest must resolve this checkout's collector too.
        child_env["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                (
                    str(Path(__file__).resolve().parents[2]),
                    child_env.get("PYTHONPATH"),
                ),
            )
        )
        for sig in (signal.SIGTERM, signal.SIGINT):
            old_signals[sig] = signal.signal(sig, forward)
        process = subprocess.Popen(
            command,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        for stream, destination, name in (
            (process.stdout, sys.stdout, "stdout.txt"),
            (process.stderr, sys.stderr, "stderr.txt"),
        ):
            thread = threading.Thread(target=copy, args=(stream, destination, name), daemon=True)
            thread.start()
            threads.append(thread)
        result = process.wait()
        record.update(
            status="incomplete" if interrupted or result < 0 else "finished", exit_code=result
        )
        return result
    except BaseException as exc:
        record["error_type"] = type(exc).__name__
        record["error"] = clean(str(exc))
        raise
    finally:
        if process is not None:
            # Descendants must not keep pipes or services alive after this command ends.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            for thread in threads:
                thread.join(timeout=5)
            record["output_complete"] = all(not thread.is_alive() for thread in threads)
        for sig, handler in old_signals.items():
            signal.signal(sig, handler)
        stack.close()
        record["ended_at_ns"] = time.time_ns()
        record["artifacts"] = inventory(directory)
        write_json(directory / "attempt.json", record)
