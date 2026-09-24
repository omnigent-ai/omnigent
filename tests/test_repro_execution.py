"""Evidence must survive failures, reset, and teardown without changing outcomes."""

import json
import os
import sys

import httpx
import pytest

from dev.repro_env.execution import Journal, inventory, run
from dev.repro_env.pytest_evidence import Evidence
from tests._helpers.repro_evidence import events


def test_failed_and_successful_commands_keep_separate_records(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "execution-context.json").write_text(
        json.dumps({"run_id": "run/1", "plan_sha256": "accepted"})
    )
    monkeypatch.setenv("EXAMPLE_API_KEY", "sensitive-example-value")
    script = tmp_path / "journey.py"
    script.write_text(
        "import os,sys; print(os.environ['EXAMPLE_API_KEY']); "
        "print('observed bug',file=sys.stderr); sys.exit(3)"
    )
    assert run(tmp_path, [sys.executable, str(script)], dict(os.environ)) == 3
    assert run(tmp_path, [sys.executable, "-c", "print('second attempt')"], dict(os.environ)) == 0
    records = [json.loads(p.read_text()) for p in (tmp_path / "execution").glob("*/attempt.json")]
    assert {r["exit_code"] for r in records} == {0, 3}
    assert len({r["attempt_id"] for r in records}) == 2
    for record in records:
        directory = tmp_path / "execution" / record["attempt_id"]
        assert record["context"]["plan_sha256"] == "accepted"
        assert record["ended_at_ns"] >= record["started_at_ns"]
        assert record["artifacts"] == inventory(directory)
        assert "sensitive-example-value" not in (directory / "stdout.txt").read_text()
    failed = next(r for r in records if r["exit_code"] == 3)
    assert failed["command_files"][0]["sha256"]
    assert (
        "observed bug"
        in (tmp_path / "execution" / failed["attempt_id"] / "stderr.txt").read_text()
    )


def test_start_failure_retains_incomplete_attempt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "execution-context.json").write_text("{}")
    with pytest.raises(FileNotFoundError):
        run(tmp_path, ["/does-not-exist"], dict(os.environ))
    record = json.loads(next((tmp_path / "execution").glob("*/attempt.json")).read_text())
    assert record["status"] == "incomplete"
    assert record["error_type"] == "FileNotFoundError"


@pytest.mark.parametrize("userinfo", ["", "fixture-user:inline-password@"])
def test_snapshots_before_delete_and_reset(tmp_path, userinfo):
    state = {"deleted": False, "reset": False}
    origin = f"http://{userinfo}localhost"

    def handle(request):
        path = request.url.path
        if request.method == "DELETE":
            state["deleted"] = True
        if path == "/mock/reset":
            state["reset"] = True
        if path == "/mock/requests":
            return httpx.Response(
                200,
                json={
                    "requests": []
                    if state["reset"]
                    else [{"model": "fixture-model", "input": "native input"}]
                },
            )
        if path == "/v1/sessions/s/items":
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "turn-1", "type": "function_call", "name": "fixture_tool"}],
                    "has_more": False,
                },
            )
        if path == "/v1/sessions/s/resources":
            return httpx.Response(200, json={"data": [{"id": "terminal-1", "type": "terminal"}]})
        return httpx.Response(200, json={"id": "s"})

    collector = Evidence(tmp_path)
    collector.install_http()
    # Keep snapshots on the same in-memory server, exercising the observer's real HTTP hooks.
    original_init = httpx.Client.__init__

    def init(client, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        original_init(client, *args, **kwargs)

    collector.patch.setattr(httpx.Client, "__init__", init)
    try:
        with httpx.Client() as client:
            client.post(f"{origin}/v1/sessions", json={"agent": "fixture"})
            client.delete(f"{origin}/v1/sessions/s")
            client.post("http://localhost/mock/reset")
        saved = events(tmp_path)
        assert "inline-password" not in json.dumps(saved)
        assert "fixture-user" not in json.dumps(saved)
        assert state == {"deleted": True, "reset": True}
        items = next(e for e in saved if e["kind"] == "session_items")
        assert items["reason"] == "before_session_delete"
        assert items["body"]["data"][0]["id"] == "turn-1"
        assert next(e for e in saved if e["kind"] == "mock_requests")["body"]["requests"]
    finally:
        collector.patch.undo()


def test_inventory_ignores_links_and_journal_marks_truncation(tmp_path):
    (tmp_path / "link").symlink_to("/etc")
    Journal(tmp_path).emit("large", payload="x" * 300000)
    assert len(inventory(tmp_path)) == 1
    assert events(tmp_path)[0]["truncated"] is True


def test_mock_reset_cannot_erase_provider_evidence(tmp_path, monkeypatch):
    from tests.server.integration import mock_llm_server as mock

    monkeypatch.setenv("OMNIGENT_REPRO_ATTEMPT_DIR", str(tmp_path))
    mock._record_evidence("request", {"model": "fixture-model", "input": "hello"})
    mock.MockState().reset()
    assert any(e.get("action") == "request" for e in events(tmp_path))
    assert any(e.get("action") == "reset" for e in events(tmp_path))


def test_pytest_plugin_autoload_preserves_failed_test_before_teardown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "execution-context.json").write_text('{"plan_sha256":"accepted"}')
    source = tmp_path / "test_journey.py"
    source.write_text("""
import httpx
import pytest

@pytest.fixture
def session(monkeypatch):
    original = httpx.Client.__init__
    def handle(request):
        if request.url.path.endswith('/items'):
            return httpx.Response(200, json={
                'data': [{'id': 'turn-observed', 'type': 'message'}], 'has_more': False
            })
        return httpx.Response(200, json={'id': 'product-session'})
    def init(client, *args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(handle)
        original(client, *args, **kwargs)
    monkeypatch.setattr(httpx.Client, '__init__', init)
    with httpx.Client() as client:
        client.post('http://localhost/v1/sessions', json={'agent': 'fixture'})
        yield
        client.delete('http://localhost/v1/sessions/product-session')

def test_failed(session):
    assert False, 'reported symptom observed'

def test_unrelated():
    assert True
""")
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    env.pop("PYTHONPATH", None)
    # Avoid inheriting this outer test run's plugins and runtime records.
    env.pop("PYTEST_PLUGINS", None)
    assert (
        run(
            tmp_path,
            [
                sys.executable,
                "-m",
                "pytest",
                str(source),
                "-q",
                "-o",
                "addopts=",
                "--confcutdir",
                str(tmp_path),
            ],
            env,
        )
        == 1
    )
    attempt = next((tmp_path / "execution").glob("*/attempt.json"))
    saved = events(attempt.parent)
    failures = [e for e in saved if e["kind"] == "test_result" and e["outcome"] == "failed"]
    assert len(failures) == 1 and failures[0]["stage"] == "call"
    items = [e for e in saved if e["kind"] == "session_items"]
    assert {e["reason"] for e in items} == {"after_test_before_teardown", "before_session_delete"}
    assert all("test_failed:" in e["test_id"] for e in items)
    assert all(e["body"]["data"][0]["id"] == "turn-observed" for e in items)
    sources = [e for e in saved if e.get("kind_of_artifact") == "test_source"]
    assert len(sources) == 2
    assert len({e["path"] for e in sources}) == 1
    assert len(list(attempt.parent.glob("source-*.py"))) == 1
    # This test runs outside a Git checkout; test-level collection must still work.
    assert {e["operation"] for e in saved if e["kind"] == "collection_error"} <= {
        "changed_files",
        "tracked_diff",
    }


def test_execute_records_readiness_failure_before_command(tmp_path, monkeypatch):
    from dev.repro_env.__main__ import execute

    monkeypatch.chdir(tmp_path)
    (tmp_path / "execution-context.json").write_text("{}")
    (tmp_path / "environment.json").write_text('{"status":"stopped"}')
    with pytest.raises(RuntimeError, match="stopped"):
        execute(tmp_path, [sys.executable, "-c", "raise AssertionError('should not run')"])
    record = json.loads(next((tmp_path / "execution").glob("*/attempt.json")).read_text())
    assert record["status"] == "incomplete"
    assert record["error_type"] == "RuntimeError"
    assert "exit_code" not in record


def test_execute_without_context_uses_existing_command_path(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from dev.repro_env import __main__ as cli

    monkeypatch.setattr(cli, "command_environment", lambda output: nullcontext(dict(os.environ)))
    assert cli.execute(tmp_path, [sys.executable, "-c", "pass"]) == 0
    assert not (tmp_path / "execution").exists()


def test_trace_text_redaction_and_uncompressed_scan(tmp_path, monkeypatch):
    import zipfile

    from dev.repro_env.execution import sanitize_trace

    monkeypatch.setenv("TEST_API_KEY", "test-private-credential")
    path = tmp_path / "trace.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "test.network",
            json.dumps(
                {
                    "headers": [{"name": "authorization", "value": "Basic private"}],
                    "text": "test-private-credential",
                }
            ),
        )
        archive.writestr("resources/response", "plaintext-marker")
    sanitize_trace(path)
    assert b"plaintext-marker" in path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        data = archive.read("test.network")
        assert b"private" not in data
        assert all(member.compress_type == zipfile.ZIP_STORED for member in archive.infolist())


def test_interrupted_command_remains_incomplete(tmp_path, monkeypatch):
    import signal

    monkeypatch.chdir(tmp_path)
    (tmp_path / "execution-context.json").write_text("{}")
    code = "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"
    assert run(tmp_path, [sys.executable, "-c", code], dict(os.environ)) == -signal.SIGTERM
    record = json.loads(next((tmp_path / "execution").glob("*/attempt.json")).read_text())
    assert record["status"] == "incomplete"
    assert record["output_complete"]


def test_lowercase_secrets_are_snapshotted_once(tmp_path, monkeypatch):
    from dev.repro_env import execution

    monkeypatch.setenv("my_api_key", "private-lowercase-key")
    journal = Journal(tmp_path)
    monkeypatch.setattr(
        execution, "secret_values", lambda env: pytest.fail("rescanned environment")
    )
    journal.emit("sample", nested=[{"text": "private-lowercase-key"}])
    assert "private-lowercase-key" not in journal.path.read_text()
    assert events(tmp_path)[0]["nested"][0]["text"] == "[redacted]"


def test_truncated_event_really_fits_byte_limit(tmp_path):
    from dev.repro_env.execution import MAX_EVENT

    journal = Journal(tmp_path)
    journal.emit("escaped", payload='"\\\n\u2603' * MAX_EVENT)
    assert len(journal.path.read_bytes()) <= MAX_EVENT
    assert events(tmp_path)[0]["truncated"]


def test_unreadable_metadata_does_not_prevent_execution(tmp_path, monkeypatch):
    import subprocess

    from dev.repro_env import execution

    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    (tmp_path / "execution-context.json").write_text("{}")
    blocked = tmp_path / "unreadable.txt"
    blocked.write_text("untracked")
    digest = execution.digest_file

    def unreadable(path):
        if path == blocked:
            raise PermissionError("unreadable")
        return digest(path)

    monkeypatch.setattr(execution, "digest_file", unreadable)
    assert run(tmp_path, [sys.executable, "-c", "print('command-ran')"], dict(os.environ)) == 0
    manifest = next((tmp_path / "execution").glob("*/attempt.json"))
    assert "command-ran" in (manifest.parent / "stdout.txt").read_text()
    record = json.loads(manifest.read_text())
    assert {"operation": "file_fingerprint", "error_type": "PermissionError"} in record[
        "collection_errors"
    ]
    assert record["ended_at_ns"] >= record["started_at_ns"]


@pytest.mark.parametrize("failure", ["inventory", "write_json"])
def test_finalization_failure_preserves_exit_status(tmp_path, monkeypatch, capsys, failure):
    from dev.repro_env import execution

    monkeypatch.chdir(tmp_path)
    (tmp_path / "execution-context.json").write_text("{}")
    original = getattr(execution, failure)

    def fail(*args):
        if failure == "inventory" or "exit_code" in args[1]:
            raise OSError("storage unavailable")
        return original(*args)

    monkeypatch.setattr(execution, failure, fail)
    assert run(tmp_path, [sys.executable, "-c", "raise SystemExit(7)"], dict(os.environ)) == 7
    assert "failed: OSError" in capsys.readouterr().err
    if failure == "inventory":
        record = json.loads(next((tmp_path / "execution").glob("*/attempt.json")).read_text())
        assert record["exit_code"] == 7
        assert not record["artifacts_complete"]
        assert {"operation": "inventory", "error_type": "OSError"} in record["collection_errors"]


@pytest.mark.parametrize("broken_journal", [False, True])
def test_latin1_test_runs_even_with_broken_evidence_sink(tmp_path, monkeypatch, broken_journal):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "execution-context.json").write_text("{}")
    source = tmp_path / "test_encoding.py"
    source.write_bytes(
        b"# coding: latin-1\ndef test_encoding():\n    assert 'caf\xe9'.endswith('\xe9')\n"
    )
    if broken_journal:
        (tmp_path / "conftest.py").write_text("""
from pathlib import Path
original = Path.open

def broken(self, *args, **kwargs):
    if self.name.startswith('events-'):
        raise OSError('cannot write evidence')
    return original(self, *args, **kwargs)

Path.open = broken
""")
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_PLUGINS": ""}
    assert (
        run(
            tmp_path,
            [
                sys.executable,
                "-m",
                "pytest",
                str(source),
                "-q",
                "-o",
                "addopts=",
                "--confcutdir",
                str(tmp_path),
            ],
            env,
        )
        == 0
    )
    attempt = next((tmp_path / "execution").glob("*/attempt.json")).parent
    assert list(attempt.glob("source-*.py"))
    if broken_journal:
        assert "journal_write failed: OSError" in (attempt / "stderr.txt").read_text()
    else:
        assert not [
            e for e in events(attempt) if e["kind"] == "collection_error" and e.get("test_id")
        ]


@pytest.mark.parametrize("kind", ["screenshot", "playwright_trace", "video", "test_source"])
def test_failed_capture_never_advertises_an_artifact(tmp_path, kind):
    collector = Evidence(tmp_path)
    collector.node = "test-1"

    def fail():
        raise OSError("capture failed")

    collector.artifact(kind, tmp_path / "missing", fail, kind_of_artifact=kind)
    saved = events(tmp_path)
    assert not any(e["kind"] == "artifact" for e in saved)
    assert saved[0]["kind"] == "collection_error"
    assert saved[0]["test_id"] == "test-1"


def test_non_session_urls_do_not_trigger_snapshots(tmp_path):
    collector = Evidence(tmp_path)
    for url in (
        "http://localhost/assets/c/app.js",
        "http://localhost/c/s/asset",
        "http://localhost/assets/v1/sessions/no",
    ):
        collector.session(url)
    assert not collector.sessions
    collector.session("http://localhost/c/s/")
    collector.session("http://localhost/v1/sessions/other/items")
    assert set(collector.sessions) == {("http://localhost", "s"), ("http://localhost", "other")}


@pytest.mark.skipif(not hasattr(os, "WNOWAIT"), reason="requires waitid with WNOWAIT")
def test_cleanup_signals_only_while_child_identity_is_reserved(tmp_path, monkeypatch):
    from dev.repro_env import execution

    monkeypatch.chdir(tmp_path)
    (tmp_path / "execution-context.json").write_text("{}")
    killpg = os.killpg
    signaled = []

    def checked(pid, sig):
        # WNOWAIT observes without reaping; this fails if cleanup already reaped the child.
        status = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        assert status.si_pid == pid
        signaled.append(pid)
        killpg(pid, sig)

    monkeypatch.setattr(execution.os, "killpg", checked)
    assert run(tmp_path, [sys.executable, "-c", "raise SystemExit(7)"], dict(os.environ)) == 7
    assert len(signaled) == 1


@pytest.mark.skipif(not hasattr(os, "WNOWAIT"), reason="requires waitid with WNOWAIT")
def test_repeated_signals_defer_journal_io(tmp_path, monkeypatch):
    import signal
    import time

    from dev.repro_env import execution

    monkeypatch.chdir(tmp_path)
    (tmp_path / "execution-context.json").write_text("{}")
    waitid, emit = os.waitid, Journal.emit
    inside_handler = False

    def checked_emit(self, *args, **kwargs):
        assert not inside_handler
        return emit(self, *args, **kwargs)

    def interrupt(*args):
        nonlocal inside_handler
        deadline = time.monotonic() + 5
        while not (tmp_path / "ready").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        handler = signal.getsignal(signal.SIGTERM)
        inside_handler = True
        try:
            handler(signal.SIGTERM, None)
            handler(signal.SIGTERM, None)
        finally:
            inside_handler = False
        return waitid(*args)

    monkeypatch.setattr(Journal, "emit", checked_emit)
    monkeypatch.setattr(execution.os, "waitid", interrupt)
    assert (
        run(
            tmp_path,
            [
                sys.executable,
                "-c",
                (
                    "import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "pathlib.Path('ready').touch(); time.sleep(30)"
                ),
            ],
            dict(os.environ),
        )
        == -signal.SIGKILL
    )
    attempt = next((tmp_path / "execution").glob("*/attempt.json")).parent
    assert len([e for e in events(attempt) if e["kind"] == "signal"]) == 2


def test_live_descendant_output_is_not_hashed(tmp_path, monkeypatch):
    import signal
    import time

    from dev.repro_env import execution

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(execution, "OUTPUT_JOIN_TIMEOUT", 0.05)
    (tmp_path / "execution-context.json").write_text("{}")
    script = tmp_path / "fork.py"
    script.write_text("""
import os, signal, time
from pathlib import Path
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path('descendant.pid').write_text(str(os.getpid()))
    for _ in range(200):
        print('still writing', flush=True)
        time.sleep(.05)
    os._exit(0)
while not Path('descendant.pid').exists():
    time.sleep(.01)
os._exit(7)
""")
    try:
        assert run(tmp_path, [sys.executable, str(script)], dict(os.environ)) == 7
        record = json.loads(next((tmp_path / "execution").glob("*/attempt.json")).read_text())
        assert not record["output_complete"]
        assert not record["artifacts_complete"]
        assert record["artifacts"] == []
    finally:
        pidfile = tmp_path / "descendant.pid"
        if pidfile.exists():
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
            time.sleep(0.1)


@pytest.mark.parametrize(
    "endpoint", ["/v1/responses", "/v1/messages", "/v1/chat/completions", "/mock/reset"]
)
def test_mock_response_survives_failed_journal(tmp_path, monkeypatch, capsys, endpoint):
    from pathlib import Path

    from fastapi.testclient import TestClient

    from tests.server.integration import mock_llm_server as mock

    monkeypatch.setenv("OMNIGENT_REPRO_ATTEMPT_DIR", str(tmp_path))
    original = Path.open

    def unwritable(path, *args, **kwargs):
        if path.name.startswith("events-"):
            raise OSError("evidence disk unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unwritable)
    with TestClient(mock.app) as client:
        response = client.post(
            endpoint, json={"model": "fixture-model", "messages": [], "stream": False}
        )
    assert response.status_code == 200
    assert "journal_write failed: OSError" in capsys.readouterr().err


def test_mock_recording_runs_off_event_loop_and_outside_state_lock(tmp_path, monkeypatch):
    import asyncio
    import threading

    from tests.server.integration import mock_llm_server as mock

    monkeypatch.setenv("OMNIGENT_REPRO_ATTEMPT_DIR", str(tmp_path))
    observed = []

    async def check():
        owner = threading.get_ident()

        def record(*args, **kwargs):
            assert threading.get_ident() != owner
            assert not mock._state._lock.locked()
            observed.append(args)

        monkeypatch.setattr(mock, "_record_evidence", record)
        transport = httpx.ASGITransport(app=mock.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            for endpoint in (
                "/v1/responses",
                "/v1/messages",
                "/v1/chat/completions",
                "/mock/reset",
            ):
                response = await client.post(
                    endpoint, json={"model": "fixture-model", "stream": False}
                )
                assert response.status_code == 200

    asyncio.run(check())
    assert [args[0] for args in observed] == ["request", "request", "request", "reset"]


def test_unsanitizable_trace_is_explicitly_unavailable(tmp_path):
    from dev.repro_env.execution import sanitize_trace

    collector = Evidence(tmp_path)
    path = tmp_path / "trace.zip"
    path.write_bytes(b"broken archive with raw credentials")
    collector.artifact(
        "trace_stop", path, lambda: sanitize_trace(path), kind_of_artifact="playwright_trace"
    )
    assert not path.exists()
    assert not path.with_suffix(".tmp").exists()
    assert not [e for e in events(tmp_path) if e["kind"] == "artifact"]
    assert any(
        e["kind"] == "collection_error" and e["operation"] == "trace_stop"
        for e in events(tmp_path)
    )


def test_startup_signal_is_recorded_and_delivered_once(tmp_path, monkeypatch):
    import signal

    from dev.repro_env import execution

    monkeypatch.chdir(tmp_path)
    (tmp_path / "execution-context.json").write_text("{}")
    popen = execution.subprocess.Popen

    def during_spawn(*args, **kwargs):
        process = popen(*args, **kwargs)
        # Arrival before run() receives the Popen result must be queued once.
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return process

    # Metadata uses subprocess.run/Popen too; only interrupt the actual command.
    def spawn(*args, **kwargs):
        return (
            during_spawn(*args, **kwargs)
            if kwargs.get("start_new_session")
            else popen(*args, **kwargs)
        )

    monkeypatch.setattr(execution.subprocess, "Popen", spawn)
    assert (
        run(tmp_path, [sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ))
        == -signal.SIGTERM
    )
    attempt = next((tmp_path / "execution").glob("*/attempt.json")).parent
    signals = [e for e in events(attempt) if e["kind"] == "signal"]
    assert len(signals) == 1
    assert signals[0]["number"] == signals[0]["requested_delivery"] == signal.SIGTERM


@pytest.mark.parametrize("supports_waitid", [True, False])
def test_exception_after_spawn_terminates_and_reaps_child(tmp_path, monkeypatch, supports_waitid):
    from dev.repro_env import execution

    monkeypatch.chdir(tmp_path)
    (tmp_path / "execution-context.json").write_text("{}")
    popen = execution.subprocess.Popen
    children = []

    def spawn(*args, **kwargs):
        process = popen(*args, **kwargs)
        if kwargs.get("start_new_session"):
            children.append(process)
        return process

    def fail_start(thread):
        raise RuntimeError("output thread could not start")

    monkeypatch.setattr(execution.subprocess, "Popen", spawn)
    monkeypatch.setattr(execution.threading.Thread, "start", fail_start)
    if not supports_waitid:
        monkeypatch.delattr(execution.os, "WNOWAIT", raising=False)
    with pytest.raises(RuntimeError, match="could not start"):
        run(tmp_path, [sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ))
    assert len(children) == 1
    assert children[0].returncode is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)
    record = json.loads(next((tmp_path / "execution").glob("*/attempt.json")).read_text())
    assert record["status"] == "incomplete"
    assert record["error_type"] == "RuntimeError"
    assert record["cleanup_exit_code"] is not None


def test_safe_url_preserves_ipv6_and_removes_credentials():
    from dev.repro_env.execution import safe_url

    assert (
        safe_url("http://user:password@[::1]:8080/v1/sessions/s?token=hidden#part")
        == "http://[::1]:8080/v1/sessions/s"
    )
    assert safe_url("http://[::1]/c/s") == "http://[::1]/c/s"


def test_inline_credentials_are_redacted_but_token_counts_survive(tmp_path):
    journal = Journal(tmp_path)
    journal.emit(
        "input",
        command=[
            "tool",
            "--password",
            "inline-password",
            "--api-key=inline-key",
            "--max-tokens",
            "4096",
        ],
        body=json.dumps(
            {
                "password": "json-password",
                "access_token": "json-token",
                "max_tokens": 1024,
                "max_output_tokens": 256,
                "usage": {
                    "input_tokens": 17,
                    "output_tokens": 11,
                    "input_tokens_details": {"cached_tokens": 3},
                },
            }
        ),
    )
    raw = journal.path.read_text()
    for secret in ("inline-password", "inline-key", "json-password", "json-token"):
        assert secret not in raw
    event = events(tmp_path)[0]
    assert event["command"] == [
        "tool",
        "--password",
        "[redacted]",
        "--api-key=[redacted]",
        "--max-tokens",
        "4096",
    ]
    body = json.loads(event["body"])
    assert body["max_tokens"] == 1024
    assert body["max_output_tokens"] == 256
    assert body["usage"] == {
        "input_tokens": 17,
        "output_tokens": 11,
        "input_tokens_details": {"cached_tokens": 3},
    }


def test_concurrent_mock_requests_share_one_journal(tmp_path, monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from dev.repro_env import execution
    from tests.server.integration import mock_llm_server as mock

    monkeypatch.setenv("OMNIGENT_REPRO_ATTEMPT_DIR", str(tmp_path))
    barrier = threading.Barrier(8)
    journals = []

    class SlowJournal(Journal):
        def __init__(self, *args):
            time.sleep(0.01)
            super().__init__(*args)
            journals.append(self)

    monkeypatch.setattr(execution, "Journal", SlowJournal)

    def request(index):
        barrier.wait(timeout=5)
        mock._record_evidence("request", {"index": index})

    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(request, range(8)))
    assert len(journals) == 1
    assert len(list(tmp_path.glob("events-*.jsonl"))) == 1
    assert {e["body"]["index"] for e in events(tmp_path)} == set(range(8))


def test_attempt_metadata_uses_the_same_redaction_as_events(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("execution-context.json", "launch-observations.json"):
        (tmp_path / name).write_text(
            json.dumps(
                {
                    "run_id": "retained-id",
                    "headers": {"authorization": "Basic inline-credential"},
                    "password": "metadata-password",
                }
            )
        )
    assert run(tmp_path, [sys.executable, "-c", "pass"]) == 0
    path = next((tmp_path / "execution").glob("*/attempt.json"))
    assert "inline-credential" not in path.read_text()
    assert "metadata-password" not in path.read_text()
    record = json.loads(path.read_text())
    assert record["context"]["run_id"] == record["runtime_launch"]["run_id"] == "retained-id"


@pytest.mark.parametrize("error", [ChildProcessError, OSError])
def test_group_wait_failure_preserves_command_result(tmp_path, monkeypatch, error):
    from dev.repro_env import execution

    monkeypatch.chdir(tmp_path)
    (tmp_path / "execution-context.json").write_text("{}")

    def fail(*args):
        raise error("group wait unavailable")

    monkeypatch.setattr(execution.os, "waitid", fail)
    monkeypatch.setattr(
        execution.os, "killpg", lambda *args: pytest.fail("unowned group signaled")
    )
    assert (
        run(tmp_path, [sys.executable, "-c", "import time; time.sleep(.1); raise SystemExit(7)"])
        == 7
    )
    record = json.loads(next((tmp_path / "execution").glob("*/attempt.json")).read_text())
    assert record["status"] == "finished"
    assert {"operation": "group_wait", "error_type": error.__name__} in record["collection_errors"]


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_trace_redaction_preserves_plaintext_line_boundaries(tmp_path, ending):
    import zipfile

    from dev.repro_env.execution import sanitize_trace

    path = tmp_path / "trace.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("resources/text", f"password=inline-secret{ending}next-line{ending}")
    sanitize_trace(path)
    with zipfile.ZipFile(path) as archive:
        assert (
            archive.read("resources/text").decode()
            == f"password=[redacted]{ending}next-line{ending}"
        )


def test_mock_acceptance_order_survives_reordered_writes(tmp_path, monkeypatch):
    import asyncio
    import threading

    from tests.server.integration import mock_llm_server as mock

    monkeypatch.setenv("OMNIGENT_REPRO_ATTEMPT_DIR", str(tmp_path))
    request_waiting = threading.Event()
    reset_written = threading.Event()
    record = mock._record_evidence

    def reordered(kind, body, accepted_at_ns, **kwargs):
        if kind == "request":
            request_waiting.set()
            assert reset_written.wait(timeout=5)
        record(kind, body, accepted_at_ns, **kwargs)
        if kind == "reset":
            reset_written.set()

    monkeypatch.setattr(mock, "_record_evidence", reordered)

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock.app), base_url="http://localhost"
        ) as client:
            request = asyncio.create_task(client.post("/v1/responses", json={"stream": False}))
            try:
                assert await asyncio.to_thread(request_waiting.wait, 5)
                assert (await client.post("/mock/reset")).status_code == 200
                assert (await request).status_code == 200
            finally:
                reset_written.set()
                response = await request
                assert response.status_code == 200

    asyncio.run(check())
    saved = events(tmp_path)
    assert [e["action"] for e in saved] == ["reset", "request"]
    assert saved[1]["accepted_at_ns"] < saved[0]["accepted_at_ns"]
    assert all(e["accepted_at_ns"] <= e["time_ns"] for e in saved)


def test_runtime_mock_module_records_without_pythonpath(tmp_path):
    import socket
    import subprocess
    import time
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    (tmp_path / "execution-context.json").write_text("{}")
    env = {**os.environ, "OMNIGENT_REPRO_EVIDENCE_ROOT": str(tmp_path)}
    for key in ("PYTHONPATH", "OMNIGENT_REPRO_ATTEMPT_DIR"):
        env.pop(key, None)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with (tmp_path / "model.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.server.integration.mock_llm_server", str(port)],
            cwd=root,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=1
            ) as client:
                deadline = time.monotonic() + 10
                while True:
                    assert process.poll() is None, (tmp_path / "model.log").read_text()
                    try:
                        if client.get("/stats").status_code == 200:
                            break
                    except httpx.TransportError:
                        # Startup can briefly refuse connections before the listener binds.
                        pass
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                assert client.post("/v1/responses", json={"stream": False}).status_code == 200
                assert client.post("/mock/reset").status_code == 200
            saved = events(tmp_path / "execution/service")
            assert [e["action"] for e in saved] == ["request", "reset"]
            assert saved[0]["accepted_at_ns"] < saved[1]["accepted_at_ns"]
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("payload", ["known", "bearer", "json"])
def test_output_redacts_across_read_boundaries(tmp_path, monkeypatch, stream, payload):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "execution-context.json").write_text("{}")
    env = {**os.environ, "BOUNDARY_API_KEY": "synthetic-boundary-credential"}
    expressions = {
        "known": "'x' * 65530 + os.environ['BOUNDARY_API_KEY']",
        "bearer": "'x' * 65525 + ' Bearer runtime-credential'",
        "json": "json.dumps({'padding': 'x' * 65500, 'password': 'runtime-credential'})",
    }
    command = f"import sys,os,json; sys.{stream}.write({expressions[payload]})"
    assert run(tmp_path, [sys.executable, "-c", command], env) == 0
    attempt = next((tmp_path / "execution").glob("*/attempt.json")).parent
    text = (attempt / f"{stream}.txt").read_text()
    assert "synthetic-boundary-credential" not in text
    assert "runtime-credential" not in text
    assert "[redacted]" in text


def test_oversized_output_line_is_omitted_without_losing_following_lines(tmp_path, monkeypatch):
    from dev.repro_env import execution

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(execution, "MAX_OUTPUT_LINE", 32)
    (tmp_path / "execution-context.json").write_text("{}")
    assert run(tmp_path, [sys.executable, "-c", "print('x' * 100); print('retained')"]) == 0
    attempt = next((tmp_path / "execution").glob("*/attempt.json")).parent
    assert (attempt / "stdout.txt").read_text() == "retained\n"
    assert any(e["kind"] == "output_omitted" and e["bytes"] == 101 for e in events(attempt))
    assert not json.loads((attempt / "attempt.json").read_text())["output_complete"]


def test_failed_output_storage_does_not_claim_truncation(tmp_path, monkeypatch):
    from pathlib import Path

    from dev.repro_env import execution

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(execution, "MAX_OUTPUT", 10)
    (tmp_path / "execution-context.json").write_text("{}")
    original = Path.open

    def open_file(path, *args, **kwargs):
        if path.name == "stdout.txt":
            raise OSError("storage unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    assert run(tmp_path, [sys.executable, "-c", "print('x' * 100)"]) == 0
    attempt = next((tmp_path / "execution").glob("*/attempt.json")).parent
    assert not any(e["kind"] == "output_truncated" for e in events(attempt))


def test_pretty_json_trace_redacts_nested_credentials(tmp_path):
    import zipfile

    from dev.repro_env.execution import sanitize_trace

    path = tmp_path / "trace.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "resources/body",
            json.dumps({"nested": {"password": "runtime-secret", "max_tokens": 17}}, indent=2),
        )
    sanitize_trace(path, secrets=())
    with zipfile.ZipFile(path) as archive:
        body = json.loads(archive.read("resources/body"))
    assert body == {"nested": {"password": "[redacted]", "max_tokens": 17}}


def test_text_redaction_keeps_counts_but_removes_url_and_header_credentials():
    from dev.repro_env.execution import clean

    assert clean("max_tokens=4096\r\n", ()) == "max_tokens=4096\r\n"
    assert clean("http://user:pass@localhost/p?a=1&token=inline-value", ()) == "http://localhost/p"
    assert clean("X-Api-Key: inline-value\n", ()) == "X-Api-Key: [redacted]\n"


def test_missing_optional_plugin_dependency_does_not_fail_pytest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "execution-context.json").write_text("{}")
    (tmp_path / "sitecustomize.py").write_text("""
import sys
class BlockHttpx:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "httpx":
            raise ModuleNotFoundError("optional httpx unavailable")
sys.meta_path.insert(0, BlockHttpx())
""")
    source = tmp_path / "test_simple.py"
    source.write_text("def test_simple():\n    assert True\n")
    env = {
        **os.environ,
        "PYTHONPATH": str(tmp_path),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_PLUGINS": "",
    }
    assert (
        run(
            tmp_path,
            [
                sys.executable,
                "-m",
                "pytest",
                str(source),
                "-q",
                "-o",
                "addopts=",
                "--confcutdir",
                str(tmp_path),
            ],
            env,
        )
        == 0
    )
    attempt = next((tmp_path / "execution").glob("*/attempt.json")).parent
    assert any(
        e["kind"] == "collection_error" and e["operation"] == "pytest_plugin_load"
        for e in events(attempt)
    )
    assert "1 passed" in (attempt / "stdout.txt").read_text()


def test_worker_http_request_is_observed_during_snapshot(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered, release = threading.Event(), threading.Event()
    collector = Evidence(tmp_path)
    original = httpx.Client.__init__

    def handle(request):
        if request.url.path == "/v1/sessions/first":
            entered.set()
            assert release.wait(timeout=5)
        return httpx.Response(200, json={"id": "second", "data": [], "has_more": False})

    def init(client, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        original(client, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", init)
    collector.install_http()
    collector.session("http://localhost/c/first")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            snapshot = pool.submit(collector.snapshot, "test")
            try:
                assert entered.wait(timeout=5)
                with httpx.Client() as client:
                    client.post("http://localhost/v1/sessions", json={})
                assert ("http://localhost", "second") in collector.sessions
            finally:
                release.set()
                snapshot.result(timeout=5)
        assert any(e["kind"] == "http" and e["method"] == "POST" for e in events(tmp_path))
    finally:
        collector.patch.undo()


def test_streamed_http_keeps_metadata_without_consuming_response(tmp_path):
    consumed = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            consumed.append(True)
            yield b"response-body"

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            list(request.stream)
            return httpx.Response(200, stream=Body())

    collector = Evidence(tmp_path)
    collector.install_http()
    try:
        with httpx.Client(transport=Transport()) as client:
            with client.stream(
                "POST", "http://localhost/v1/sessions/s/items", content=iter([b"input"])
            ) as response:
                assert not consumed
                assert response.read() == b"response-body"
        event = next(e for e in events(tmp_path) if e["kind"] == "http")
        assert event["status"] == 200
        assert event["body_unavailable"] == {"request": "streamed", "response": "streamed"}
    finally:
        collector.patch.undo()


def test_disabled_mock_evidence_does_not_schedule_worker(tmp_path, monkeypatch):
    import asyncio

    from tests.server.integration import mock_llm_server as mock

    monkeypatch.delenv("OMNIGENT_REPRO_ATTEMPT_DIR", raising=False)
    monkeypatch.setenv("OMNIGENT_REPRO_EVIDENCE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        mock.asyncio, "to_thread", lambda *args: pytest.fail("disabled evidence scheduled work")
    )
    asyncio.run(mock._record_evidence_async("request", {}))
    assert not list(tmp_path.glob("**/events-*.jsonl"))


def test_output_byte_limit_preserves_valid_utf8(tmp_path, monkeypatch):
    from dev.repro_env import execution

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(execution, "MAX_OUTPUT", 7)
    (tmp_path / "execution-context.json").write_text("{}")
    assert run(tmp_path, [sys.executable, "-c", "print(chr(0x2603) * 10)"]) == 0
    attempt = next((tmp_path / "execution").glob("*/attempt.json")).parent
    data = (attempt / "stdout.txt").read_bytes()
    assert data.decode("utf-8") == "\u2603\u2603"
    assert len(data) <= 7
    event = next(e for e in events(attempt) if e["kind"] == "output_truncated")
    assert event["saved_bytes"] == len(data)


def test_trace_cleanup_attempts_both_paths_and_preserves_original_error(tmp_path, monkeypatch):
    import zipfile
    from pathlib import Path

    from dev.repro_env.execution import sanitize_trace

    path = tmp_path / "trace.zip"
    path.write_bytes(b"invalid zip")
    attempts = []

    def denied(path, **kwargs):
        attempts.append(path)
        raise PermissionError("cannot remove")

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(zipfile.BadZipFile):
        sanitize_trace(path)
    assert attempts == [path, path.with_suffix(".tmp")]
