"""Evidence must survive failures, reset, and teardown without changing outcomes."""

import json
import os
import sys

import httpx
import pytest

from dev.repro_env.execution import Journal, inventory, run
from dev.repro_env.pytest_evidence import Evidence


def events(path):
    return [
        json.loads(line)
        for file in path.glob("events-*.jsonl")
        for line in file.read_text().splitlines()
    ]


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
    import pytest

    monkeypatch.chdir(tmp_path)
    (tmp_path / "execution-context.json").write_text("{}")
    with pytest.raises(FileNotFoundError):
        run(tmp_path, ["/does-not-exist"], dict(os.environ))
    record = json.loads(next((tmp_path / "execution").glob("*/attempt.json")).read_text())
    assert record["status"] == "incomplete"
    assert record["error_type"] == "FileNotFoundError"


def test_snapshots_before_delete_and_reset(tmp_path):
    state = {"deleted": False, "reset": False}

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
            client.post("http://localhost/v1/sessions", json={"agent": "fixture"})
            client.delete("http://localhost/v1/sessions/s")
            client.post("http://localhost/mock/reset")
        saved = events(tmp_path)
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
    from pathlib import Path

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
                str(Path(sys.executable).with_name("pytest")),
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
    # This test runs outside a Git checkout; test-level collection must still work.
    assert {e["operation"] for e in saved if e["kind"] == "collection_error"} <= {
        "changed_files",
        "tracked_diff",
    }


def test_execute_records_readiness_failure_before_command(tmp_path, monkeypatch):
    import pytest

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
        run(tmp_path, [sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ))
        == -signal.SIGTERM
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

        def record(*args):
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
