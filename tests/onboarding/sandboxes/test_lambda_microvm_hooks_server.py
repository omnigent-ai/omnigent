"""
Tests for the Lambda MicroVM hooks server's payload extraction.

The hooks server (``deploy/aws-lambda-microvm/hooks_server.py``) is a stdlib-only
script that ships inside the MicroVM image, not an importable package module, so
we load ``_extract_identity_payload`` by path and exercise its shape tolerance:
the platform's /run body framing is not contractually fixed, so the parser must
recover the identity map whether it arrives wrapped, bare, or as a JSON string.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests._helpers.polling import poll_until

_HOOKS = Path(__file__).resolve().parents[3] / "deploy" / "aws-lambda-microvm" / "hooks_server.py"


def _load_hooks() -> Any:
    spec = importlib.util.spec_from_file_location("_lambda_microvm_hooks", _HOOKS)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_hooks = _load_hooks()

_IDENTITY = {
    "OMNIGENT_SERVER": "https://srv.example.com",
    "OMNIGENT_HOST_ID": "host_abc",
    "OMNIGENT_HOST_NAME": "managed-abc",
    "OMNIGENT_HOST_TOKEN": "tok-xyz",
    "ANTHROPIC_API_KEY": "sk-test",
}


@pytest.fixture(autouse=True)
def reset_host_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(_hooks, "_host_started", False)
    monkeypatch.setattr(_hooks, "_host_env", None)
    monkeypatch.setattr(_hooks, "_host_exit_code", None)
    monkeypatch.setattr(_hooks, "_HOST_LOG_PATH", str(tmp_path / "host.log"))


@pytest.mark.parametrize(
    "body",
    [
        {"microvmId": "mv-1", "runHookPayload": json.dumps(_IDENTITY)},
        {"microvmId": "mv-1", "runHookPayload": _IDENTITY},
        _IDENTITY,
        json.dumps(_IDENTITY),
    ],
    ids=["wrapped-string", "wrapped-object", "bare", "json-string"],
)
def test_extract_identity_payload(body: Any) -> None:
    assert _hooks._extract_identity_payload(json.dumps(body).encode()) == _IDENTITY


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2, 3]", b"123"])
def test_extract_non_dict_body_returns_none(raw: bytes) -> None:
    """Non-JSON or non-dict bodies yield None (host is not started)."""
    assert _hooks._extract_identity_payload(raw) is None


@pytest.mark.parametrize("raw", [b"", b"{}", b'{"microvmId": "mv-1"}'])
def test_extract_identity_less_body_never_starts_host(raw: bytes) -> None:
    """An empty or identity-less dict body (bare {}, or {microvmId} with no
    payload) must NOT satisfy the identity gate — the required keys are absent,
    so whether the extractor returns {} or None, no host is started."""
    payload = _hooks._extract_identity_payload(raw)
    assert payload is None or any(
        not (payload or {}).get(key) for key in _hooks._REQUIRED_IDENTITY
    )


def test_spawn_failure_clears_started_flag_so_retry_can_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Popen must reset ``_host_started`` so a platform /run retry
    re-attempts the spawn. A sticky flag would strand the VM host-less: the
    guard at the top of ``_start_host_from_payload`` short-circuits every
    subsequent call."""
    monkeypatch.setattr(_hooks, "_host_started", False)
    body = json.dumps(_IDENTITY).encode()

    calls = {"n": 0}

    proc = _FakeHostProc()

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("start_host.sh not found")
        return proc  # second attempt "succeeds" and keeps running

    monkeypatch.setattr(_hooks.subprocess, "Popen", _boom)

    # First /run: spawn raises, flag must be cleared back to False.
    _hooks._start_host_from_payload(body)
    assert _hooks._host_started is False

    # Retry: guard doesn't short-circuit, spawn is attempted again and sticks.
    _hooks._start_host_from_payload(body)
    assert calls["n"] == 2
    assert _hooks._host_started is True
    proc.exit(0)
    poll_until(lambda: _hooks._host_started is False, timeout_s=5, interval_s=0.01)


class _FakeHostProc:
    """Stands in for start_host.sh: no output, and it exits when the test says so."""

    def __init__(self, stdout: _LineStream | None = None) -> None:
        self.stdout = stdout
        self.returncode = 0
        self._done = threading.Event()

    def exit(self, code: int) -> None:
        self.returncode = code
        self._done.set()

    def wait(self) -> int:
        self._done.wait(timeout=5)
        return self.returncode


@pytest.fixture
def spawned(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[tuple[_FakeHostProc, dict[str, str]]]]:
    """Reset the hooks server's host state and record every start_host.sh spawn."""
    procs: list[tuple[_FakeHostProc, dict[str, str]]] = []

    def _popen(*_args: Any, env: dict[str, str], **_kwargs: Any) -> _FakeHostProc:
        proc = _FakeHostProc()
        procs.append((proc, env))
        return proc

    monkeypatch.setattr(_hooks.subprocess, "Popen", _popen)
    yield procs
    for proc, _env in procs:
        proc.exit(0)
    # Finish the exit callback before monkeypatch restores module state.
    poll_until(lambda: not _hooks._host_started, timeout_s=5, interval_s=0.01)


def test_host_exit_clears_started_flag_and_records_status(
    spawned: list[tuple[_FakeHostProc, dict[str, str]]],
) -> None:
    """When start_host.sh ends, the latch drops so the host can be started again."""
    _hooks._start_host_from_payload(json.dumps(_IDENTITY).encode())
    assert _hooks._host_started is True
    spawned[0][0].exit(0)
    poll_until(lambda: _hooks._host_started is False, timeout_s=5, interval_s=0.01)
    assert _hooks._host_exit_code == 0


class _LineStream:
    """The host's stdout pipe: yields fixed lines and records how many were drained."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.drained = 0
        self.closed = False

    def __iter__(self) -> Iterator[str]:
        for line in self._lines:
            self.drained += 1
            yield line

    def close(self) -> None:
        self.closed = True


class _FullDiskLog:
    """A host log file whose *failing* operation raises like a full filesystem."""

    def __init__(self, failing: str) -> None:
        self._failing = failing

    def _io(self, operation: str) -> None:
        if operation == self._failing:
            raise OSError(28, "No space left on device")

    def write(self, _line: str) -> None:
        self._io("write")

    def flush(self) -> None:
        self._io("flush")

    def close(self) -> None:
        self._io("close")


_HOST_OUTPUT = [f"host line {index}\n" for index in range(5)]


def _run_host_to_exit(monkeypatch: pytest.MonkeyPatch, stream: _LineStream) -> None:
    """Spawn a host that writes *stream*, let it exit 3, and wait for the reap."""
    proc = _FakeHostProc(stdout=stream)
    monkeypatch.setattr(_hooks.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    _hooks._start_host_from_payload(json.dumps(_IDENTITY).encode())
    proc.exit(3)
    poll_until(lambda: _hooks._host_exit_code is not None, timeout_s=5, interval_s=0.01)


@pytest.mark.parametrize("failing", ["write", "flush", "close"])
def test_log_file_failure_keeps_draining_and_releases_the_latch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failing: str
) -> None:
    """A failing log file must not stop the drain (an unread pipe blocks the host)
    or skip the reap (a stuck latch makes every later /run and /resume a no-op)."""
    monkeypatch.setattr(
        _hooks, "open", lambda *_args, **_kwargs: _FullDiskLog(failing), raising=False
    )
    stream = _LineStream(_HOST_OUTPUT)

    _run_host_to_exit(monkeypatch, stream)

    assert stream.drained == len(_HOST_OUTPUT)
    assert stream.closed
    assert _hooks._host_started is False
    assert _hooks._host_exit_code == 3
    out = capsys.readouterr().out
    assert all(f"host: {line}" in out for line in _HOST_OUTPUT)


def test_stdout_failure_keeps_logging_and_releases_the_latch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken container stdout drops that sink only; the log file still gets
    every line and the host is reaped."""

    def _broken_stdout(*args: object, file: object = None, **kwargs: object) -> None:
        if file is None:
            raise OSError(32, "Broken pipe")
        print(*args, file=file, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(_hooks, "print", _broken_stdout, raising=False)
    stream = _LineStream(_HOST_OUTPUT)

    _run_host_to_exit(monkeypatch, stream)

    assert stream.drained == len(_HOST_OUTPUT)
    assert _hooks._host_started is False
    assert _hooks._host_exit_code == 3
    assert Path(_hooks._HOST_LOG_PATH).read_text() == "".join(_HOST_OUTPUT)


def test_resume_restarts_a_host_that_stood_down(
    spawned: list[tuple[_FakeHostProc, dict[str, str]]],
) -> None:
    """A host that exited cleanly before the suspend is restarted on /resume with
    the same identity, so the thawed VM can dial back with the token the server
    kept."""
    _hooks._start_host_from_payload(json.dumps(_IDENTITY).encode())
    spawned[0][0].exit(0)
    poll_until(lambda: _hooks._host_started is False, timeout_s=5, interval_s=0.01)

    _hooks._restart_host_after_resume()

    assert len(spawned) == 2
    assert spawned[1][1]["OMNIGENT_HOST_TOKEN"] == _IDENTITY["OMNIGENT_HOST_TOKEN"]
    assert spawned[1][1]["OMNIGENT_HOST_ID"] == _IDENTITY["OMNIGENT_HOST_ID"]
    assert _hooks._host_started is True


def test_resume_leaves_a_fatally_exited_host_down(
    spawned: list[tuple[_FakeHostProc, dict[str, str]]],
) -> None:
    """The fatal status means no restart can succeed, so /resume does not retry it."""
    _hooks._start_host_from_payload(json.dumps(_IDENTITY).encode())
    spawned[0][0].exit(_hooks._HOST_FATAL_EXIT_CODE)
    poll_until(lambda: _hooks._host_started is False, timeout_s=5, interval_s=0.01)

    _hooks._restart_host_after_resume()

    assert len(spawned) == 1


def test_resume_does_not_start_a_second_host(
    spawned: list[tuple[_FakeHostProc, dict[str, str]]],
) -> None:
    """A host that is still running (the normal thaw) is left alone."""
    _hooks._start_host_from_payload(json.dumps(_IDENTITY).encode())
    _hooks._restart_host_after_resume()
    assert len(spawned) == 1


def test_resume_before_any_run_starts_nothing(
    spawned: list[tuple[_FakeHostProc, dict[str, str]]],
) -> None:
    """With no /run identity there is no host to restart."""
    _hooks._restart_host_after_resume()
    assert spawned == []


def test_fatal_exit_code_matches_the_host() -> None:
    """The image copy of the fatal status must track omnigent.host's constant."""
    from omnigent.host import HOST_FATAL_EXIT_CODE

    assert _hooks._HOST_FATAL_EXIT_CODE == HOST_FATAL_EXIT_CODE


@pytest.mark.parametrize("missing", _hooks._REQUIRED_IDENTITY)
@pytest.mark.parametrize("blank", [False, True], ids=["absent", "empty"])
def test_run_without_the_full_identity_starts_nothing(
    spawned: list[tuple[_FakeHostProc, dict[str, str]]], missing: str, blank: bool
) -> None:
    """/run starts the host only with every identity key set.

    A host missing its server, id, name, or token cannot authenticate its
    dial-back, so it would fail in the guest with nothing on the server side.
    """
    payload = {k: v for k, v in _IDENTITY.items() if k != missing}
    if blank:
        payload[missing] = ""
    _hooks._start_host_from_payload(json.dumps(payload).encode())
    assert spawned == []
    assert _hooks._host_started is False


def test_spawn_refuses_a_second_host_while_one_runs(
    spawned: list[tuple[_FakeHostProc, dict[str, str]]],
) -> None:
    """_spawn_host starts nothing while start_host.sh still runs.

    Two /run deliveries can both pass _start_host_from_payload's check before
    either spawns; this check, taken under the lock, is the one that stops the
    second from starting another host on the same token.
    """
    _hooks._start_host_from_payload(json.dumps(_IDENTITY).encode())
    _hooks._spawn_host({k: str(v) for k, v in _IDENTITY.items()})
    assert len(spawned) == 1


@pytest.fixture
def hooks_port(spawned: list[tuple[_FakeHostProc, dict[str, str]]]) -> Iterator[int]:
    """Serve the real request handler on a free local port."""
    server = _hooks.ThreadingHTTPServer(("127.0.0.1", 0), _hooks._HookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(port: int, method: str, path: str, body: Any = None) -> int:
    """Send one request to the hooks server and return its status code."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body)
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def test_run_hook_starts_the_host_with_the_posted_identity(
    hooks_port: int, spawned: list[tuple[_FakeHostProc, dict[str, str]]]
) -> None:
    """The handler reads the /run body and hands its identity to start_host.sh.

    This is the only path the identity takes into the guest.
    """
    body = json.dumps({"microvmId": "mv-1", "runHookPayload": json.dumps(_IDENTITY)})

    assert _request(hooks_port, "POST", _hooks._RUN_PATH, body.encode()) == 200

    assert len(spawned) == 1
    env = spawned[0][1]
    assert {key: env[key] for key in _IDENTITY} == _IDENTITY


def test_run_hook_reads_a_chunked_body(
    hooks_port: int, spawned: list[tuple[_FakeHostProc, dict[str, str]]]
) -> None:
    """A /run body sent with chunked transfer encoding arrives whole."""
    raw = json.dumps(_IDENTITY).encode()
    # An iterable body makes http.client send one chunk per item.
    chunks = iter([raw[:10], raw[10:]])

    assert _request(hooks_port, "POST", _hooks._RUN_PATH, chunks) == 200

    assert len(spawned) == 1
    env = spawned[0][1]
    assert {key: env[key] for key in _IDENTITY} == _IDENTITY


def test_resume_hook_restarts_a_host_that_stood_down(
    hooks_port: int, spawned: list[tuple[_FakeHostProc, dict[str, str]]]
) -> None:
    """POST /resume reaches the restart path with the identity /run delivered."""
    assert _request(hooks_port, "POST", _hooks._RUN_PATH, json.dumps(_IDENTITY).encode()) == 200
    spawned[0][0].exit(0)
    poll_until(lambda: _hooks._host_started is False, timeout_s=5, interval_s=0.01)

    assert _request(hooks_port, "POST", _hooks._RESUME_PATH) == 200

    assert len(spawned) == 2
    assert spawned[1][1]["OMNIGENT_HOST_TOKEN"] == _IDENTITY["OMNIGENT_HOST_TOKEN"]


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/aws/lambda-microvms/runtime/v1/ready", 200),
        ("POST", "/aws/lambda-microvms/runtime/v1/suspend", 200),
        ("POST", "/aws/lambda-microvms/runtime/v1/terminate", 200),
        ("POST", "/aws/lambda-microvms/runtime/v1/validate", 200),
        ("GET", "/aws/lambda-microvms/runtime/v1/run", 404),
        ("GET", "/aws/lambda-microvms/runtime/v1/redy", 404),
        ("POST", "/aws/lambda-microvms/runtime/v1/nope", 404),
    ],
)
def test_lifecycle_paths_answer_and_unknown_paths_404(
    hooks_port: int,
    spawned: list[tuple[_FakeHostProc, dict[str, str]]],
    method: str,
    path: str,
    status: int,
) -> None:
    """Platform probes get a 200; a typo'd path gets a 404 instead of looking healthy."""
    assert _request(hooks_port, method, path) == status
    assert spawned == []
