"""Evidence must survive failures, reset, and teardown without changing outcomes."""

import json
import os
import sys

import httpx

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
    assert not [e for e in saved if e["kind"] == "collection_error"]


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
