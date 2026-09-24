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
MAX_OUTPUT_LINE = 8 * 1024 * 1024
OUTPUT_JOIN_TIMEOUT = 5
PROCESS_CLEANUP_TIMEOUT = 5
SECRET_NAME = re.compile(r"authorization|cookie|password|secret|api.?key|token", re.I)


TOKEN_COUNT = re.compile(
    r"(?:max_)?(?:input_|output_|prompt_|completion_|total_|cached_|reasoning_|cache_read_|cache_creation_)?tokens",
    re.I,
)
TOKEN_DETAILS = {
    "token_usage",
    "input_tokens_details",
    "output_tokens_details",
    "prompt_tokens_details",
    "completion_tokens_details",
}


def credential_field(name, value=None):
    name = str(name).lstrip("-").replace("-", "_")
    if TOKEN_COUNT.fullmatch(name) and (
        isinstance(value, (int, float)) or (isinstance(value, str) and value.isdecimal())
    ):
        return False
    if name.lower() in TOKEN_DETAILS and isinstance(value, dict):
        return False
    return bool(SECRET_NAME.search(name))


def secret_values(env):
    return tuple(
        sorted(
            {
                value
                for name, value in env.items()
                if len(value) >= 8 and credential_field(name, value)
            },
            key=len,
            reverse=True,
        )
    )


def clean(value, secrets=None):
    """Omit credentials from structured observations; bundle scanning still applies."""
    if secrets is None:
        secrets = secret_values(os.environ)
    if isinstance(value, dict):
        if "value" in value and credential_field(value.get("name", ""), value["value"]):
            value = {**value, "value": "[redacted]"}
        return {
            k: "[redacted]" if credential_field(k, v) else clean(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            "[redacted]"
            if i
            and isinstance(value[i - 1], str)
            and value[i - 1].startswith("--")
            and "=" not in value[i - 1]
            and credential_field(value[i - 1], v)
            else clean(v, secrets)
            for i, v in enumerate(value)
        ]
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            with contextlib.suppress(ValueError):
                parsed = json.loads(value)
                if isinstance(parsed, (dict, list)):
                    return json.dumps(clean(parsed, secrets), ensure_ascii=False)
        key, separator, content = value.partition("=")
        if (
            separator
            and not any(c.isspace() for c in key)
            and credential_field(key, content.rstrip("\r\n"))
        ):
            return key + "=[redacted]" + value[len(value.rstrip("\r\n")) :]
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[redacted]", value)
        value = re.sub(r"https?://[^\s<>\"']+", lambda m: safe_url(m[0]), value)
        value = re.sub(
            r"(?im)\b((?:authorization|proxy-authorization|(?:x-)?api[-_]key|cookie|set-cookie|password|secret|(?:access[-_]|refresh[-_])?token)\s*:\s*)[^\r\n]+",
            r"\1[redacted]",
            value,
        )
    return value


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class Journal:
    """Best-effort sink: collection failures cannot change the observed result."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / f"events-{os.getpid()}-{uuid.uuid4().hex}.jsonl"
        self.lock = threading.Lock()
        self.errors = []
        self.secrets = secret_values(os.environ)
        self.capture(
            "create_directory", lambda: directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        )

    def clean(self, value):
        return clean(value, self.secrets)

    def failure(self, operation, exc):
        error = {"operation": operation, "error_type": type(exc).__name__}
        self.errors.append(error)
        with contextlib.suppress(Exception):
            print(
                f"reproduction evidence {operation} failed: {type(exc).__name__}", file=sys.stderr
            )
        return error

    def capture(self, operation, callback, **context):
        try:
            return callback()
        except Exception as exc:  # noqa: BLE001 — evidence failures cannot replace outcomes.
            self.emit("collection_error", **self.failure(operation, exc), **context)
            return None

    def emit(self, kind: str, **data) -> None:
        try:
            event = {"time_ns": time.time_ns(), "kind": kind, **self.clean(data)}
            encoded = json.dumps(event, ensure_ascii=True)
            if len(encoded) + 1 > MAX_EVENT:
                event = {
                    "time_ns": event["time_ns"],
                    "kind": kind[:256],
                    "truncated": True,
                    "original_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                    "preview": encoded[:4096],
                }
                encoded = json.dumps(event, ensure_ascii=True)
            with self.lock, self.path.open("a") as stream:
                stream.write(encoded + "\n")
        except Exception as exc:  # noqa: BLE001 — including disk exhaustion during error reporting.
            self.failure("journal_write", exc)


def sanitize_trace(path: Path, secrets=None) -> None:
    """Redact text resources and keep ZIP members visible to the bundle byte scan."""
    if secrets is None:
        secrets = secret_values(os.environ)
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
                    try:
                        data = json.dumps(clean(json.loads(content), secrets)).encode()
                    except ValueError:
                        lines = []
                        for line in content.splitlines(keepends=True):
                            try:
                                lines.append(json.dumps(clean(json.loads(line), secrets)) + "\n")
                            except ValueError:
                                lines.append(clean(line, secrets))
                        data = "".join(lines).encode()
                target.writestr(member.filename, data)
        temporary.replace(path)
    except BaseException:
        # Never retain raw credential-bearing traces in the automatically uploaded tree.
        for candidate in (path, temporary):
            with contextlib.suppress(OSError):
                candidate.unlink(missing_ok=True)
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


def run(
    output: Path, command: list[str], env: dict[str, str] | None = None, *, prepare=None
) -> int:
    """Keep the command's outcome even if optional evidence collection fails."""
    if env is not None and prepare is not None:
        raise ValueError("Supply either env or prepare, not both")
    attempt_id = uuid.uuid4().hex
    directory = output / "execution" / attempt_id
    journal = Journal(directory)
    if env is not None:
        journal.secrets = secret_values({**os.environ, **env})
    root = Path.cwd()
    record = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "started_at_ns": time.time_ns(),
        "status": "incomplete",
        "command": journal.clean(command),
        "cwd": str(root),
        "python": sys.version,
        "producer": "repro_env_exec",
        "limitations": [
            "Agent-workspace observations, not an independent verifier.",
            "Only wrapped commands and supported pytest/browser paths are instrumented.",
            "No observed event does not prove an action was absent.",
        ],
    }

    def save_record():
        record["collection_errors"] = list(journal.errors)
        journal.capture(
            "attempt_write", lambda: write_json(directory / "attempt.json", journal.clean(record))
        )

    def fingerprint(path):
        return journal.capture(
            "file_fingerprint", lambda: {"path": str(path), "sha256": digest_file(root / path)}
        )

    def git(*args):
        result = subprocess.run(["git", *args], capture_output=True, cwd=root, timeout=5)
        if result.returncode:
            raise RuntimeError("Git metadata unavailable")
        return result.stdout

    save_record()

    def collect_metadata():
        record["context"] = journal.capture(
            "execution_context",
            lambda: json.loads((output / "execution-context.json").read_text()),
        )
        record["checkout"] = journal.capture("checkout", lambda: launch_observations(root))
        plan = root / ".omnigent/reproduction-plan.json"
        record["working_plan_sha256"] = journal.capture(
            "working_plan", lambda: digest_file(plan) if plan.is_file() else None
        )
        launch = output / "launch-observations.json"
        record["runtime_launch"] = journal.capture(
            "runtime_launch", lambda: json.loads(launch.read_text()) if launch.is_file() else None
        )
        changed = journal.capture(
            "changed_files",
            lambda: git("ls-files", "-z", "--modified", "--others", "--exclude-standard"),
        )
        record["changed_files"] = []
        if changed is not None:
            for name in changed.decode(errors="replace").split("\0"):
                if name and not (root / name).is_symlink():
                    row = fingerprint(name)
                    if row is not None:
                        record["changed_files"].append(row)
        diff = journal.capture("tracked_diff", lambda: git("diff", "--binary", "HEAD"))
        record["tracked_diff_sha256"] = (
            hashlib.sha256(diff).hexdigest() if diff is not None else None
        )
        record["command_files"] = []
        for arg in command:
            if (
                not arg.startswith("-")
                and (root / arg).is_file()
                and not (root / arg).is_symlink()
            ):
                row = fingerprint(arg)
                if row is not None:
                    record["command_files"].append(row)

    journal.capture("metadata", collect_metadata)
    save_record()
    stack = contextlib.ExitStack()
    process = None
    group_owned = False
    pending_signals = []
    startup_signals = []
    old_signals = {}
    threads = []
    output_errors = []

    def deliver(signum):
        with contextlib.suppress(ProcessLookupError):
            if group_owned:
                os.killpg(process.pid, signum)
            else:
                process.send_signal(signum)

    def forward(signum, _frame):
        pending_signals.append(signum)
        requested = signum if len(pending_signals) == 1 else signal.SIGKILL
        if process is None:
            startup_signals.append(requested)
        else:
            deliver(requested)

    def reap_child():
        process.terminate()
        try:
            process.wait(timeout=PROCESS_CLEANUP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=PROCESS_CLEANUP_TIMEOUT)

    def copy(stream, destination, name):
        saved = journal.capture("output_open", lambda: (directory / name).open("wb"))
        if saved is None:
            output_errors.append(name)
        total = 0
        truncated = False
        pending = bytearray()
        omitted = 0

        def save_line():
            nonlocal total, saved, truncated
            text = journal.clean(pending.decode(errors="replace"))
            if saved is not None:
                try:
                    available = max(0, MAX_OUTPUT - total)
                    encoded = text.encode("utf-8", errors="backslashreplace")
                    retained = encoded[:available].decode("utf-8", errors="ignore").encode("utf-8")
                    saved.write(retained)
                    saved.flush()
                    truncated |= len(encoded) > len(retained)
                    total += len(retained)
                except Exception as exc:  # noqa: BLE001 — keep draining even if storage fails.
                    journal.failure("saved_output", exc)
                    output_errors.append(name)
                    with contextlib.suppress(Exception):
                        saved.close()
                    saved = None
            pending.clear()

        def finish_line():
            nonlocal omitted
            if omitted:
                journal.emit(
                    "output_omitted",
                    stream=name,
                    bytes=omitted,
                    reason="Line exceeded the bounded redaction buffer",
                )
                output_errors.append(name)
                omitted = 0
            else:
                save_line()

        try:
            for chunk in iter(lambda: stream.readline(65536), b""):
                if destination is not None:
                    try:
                        destination.write(chunk.decode(errors="replace"))
                        destination.flush()
                    except Exception as exc:  # noqa: BLE001 — keep draining the child's pipes.
                        journal.failure("console_output", exc)
                        destination = None
                # Redact complete lines; never persist independently sanitized fragments.
                if omitted or len(pending) + len(chunk) > MAX_OUTPUT_LINE:
                    omitted += len(pending) + len(chunk)
                    pending.clear()
                else:
                    pending.extend(chunk)
                if chunk.endswith(b"\n"):
                    finish_line()
            if pending or omitted:
                finish_line()
        except Exception as exc:  # noqa: BLE001 — expose partial output separately from child status.
            output_errors.append(name)
            journal.failure("output_read", exc)
        finally:
            if saved is not None:
                journal.capture("output_close", saved.close)
            journal.capture("pipe_close", stream.close)
        if truncated:
            output_errors.append(name)
            journal.emit("output_truncated", stream=name, saved_bytes=total)

    try:
        if prepare is not None:
            env = stack.enter_context(prepare())
        elif env is None:
            env = dict(os.environ)
        journal.secrets = secret_values({**os.environ, **env})
        child_env = {**env, "OMNIGENT_REPRO_ATTEMPT_DIR": str(directory.resolve())}
        plugins = [p for p in child_env.get("PYTEST_PLUGINS", "").split(",") if p]
        if not {"dev.repro_env.pytest_evidence", "dev.repro_env.pytest_loader"}.intersection(
            plugins
        ):
            plugins.append("dev.repro_env.pytest_loader")
        child_env["PYTEST_PLUGINS"] = ",".join(plugins)
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
        group_owned = hasattr(os, "waitid") and hasattr(os, "WNOWAIT")
        process = subprocess.Popen(
            command,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        while startup_signals:
            deliver(startup_signals.pop(0))
        for stream, destination, name in (
            (process.stdout, sys.stdout, "stdout.txt"),
            (process.stderr, sys.stderr, "stderr.txt"),
        ):
            thread = threading.Thread(target=copy, args=(stream, destination, name), daemon=True)
            thread.start()
            threads.append(thread)
        if group_owned:
            # Keep the leader unreaped until group cleanup: its PGID cannot be recycled.
            try:
                os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
            except OSError as exc:
                group_owned = False
                journal.failure("group_wait", exc)
            else:
                journal.capture("process_group_terminate", lambda: deliver(signal.SIGTERM))
                group_owned = False
        result = process.wait()
        record.update(
            status="incomplete" if pending_signals or result < 0 else "finished", exit_code=result
        )
        return result
    except BaseException as exc:
        record["error_type"] = type(exc).__name__
        record["error"] = journal.clean(str(exc))
        raise
    finally:
        if process is not None:
            if group_owned:
                journal.capture("process_group_terminate", lambda: deliver(signal.SIGTERM))
                group_owned = False
            if "exit_code" not in record:
                journal.capture("process_cleanup", reap_child)
                record["cleanup_exit_code"] = process.returncode
            for thread in threads:
                thread.join(timeout=OUTPUT_JOIN_TIMEOUT)
            record["output_complete"] = not output_errors and all(
                not thread.is_alive() for thread in threads
            )
        for sig, handler in old_signals.items():
            signal.signal(sig, handler)
        for index, sig in enumerate(pending_signals):
            journal.emit(
                "signal", number=sig, requested_delivery=sig if index == 0 else signal.SIGKILL
            )
        journal.capture("environment_close", stack.close)
        record["ended_at_ns"] = time.time_ns()
        # Preserve the outcome before optional hashing, which can fail independently.
        save_record()
        stable = all(not thread.is_alive() for thread in threads)
        if stable and process is not None:
            for stream in (process.stdout, process.stderr):
                journal.capture("pipe_close", stream.close)
        artifacts = journal.capture("inventory", lambda: inventory(directory)) if stable else None
        record["artifacts"] = artifacts or []
        record["artifacts_complete"] = artifacts is not None
        save_record()
