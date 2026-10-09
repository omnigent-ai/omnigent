"""Exercise evidence collection with a real browser and local test server."""

import pytest

from dev.repro_env.pytest_evidence import Evidence
from tests._helpers.repro_evidence import events


def exchange(page, url, timeout=5000):
    return page.evaluate(
        """({url, timeout}) => new Promise((resolve, reject) => {
                const socket = new WebSocket(url);
                const timer = setTimeout(() => {
                    socket.close(); reject(new Error('WebSocket response timed out'));
                }, timeout);
                socket.onopen = () => socket.send('native-input');
                socket.onerror = () => {
                    clearTimeout(timer); reject(new Error('WebSocket error'));
                };
                socket.onclose = () => {
                    clearTimeout(timer); reject(new Error('WebSocket closed before response'));
                };
                socket.onmessage = event => {
                    clearTimeout(timer); resolve(event.data); socket.close();
                };
            })""",
        {"url": url, "timeout": timeout},
    )


def test_browser_trace_preserves_actions_mock_boundary_and_video(tmp_path, browser):
    import shutil
    import threading
    import zipfile
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from websockets.sync.server import serve

    final_items = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if self.path == "/c/shared":
                self.send_header("Content-Type", "text/html")
                body = b'<input aria-label="Input"><button>Observe</button>'
            else:
                self.send_header("Content-Type", "application/json")
                import json

                body = json.dumps(
                    {"id": "shared"}
                    if self.path == "/v1/sessions/shared"
                    else {"data": final_items, "has_more": False}
                ).encode()
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def echo(socket):
        received = socket.recv(timeout=5)
        socket.send("observed-output" if received == "native-input" else f"unexpected:{received}")

    sockets = serve(echo, "127.0.0.1", 0)
    socket_thread = threading.Thread(target=sockets.serve_forever, daemon=True)
    socket_thread.start()
    collector = Evidence(tmp_path / "saved")
    collector.node = "browser-attempt"
    try:
        collector.install_browser()
        with browser.new_context(record_video_dir=str(tmp_path / "temporary-video")) as context:
            page = context.new_page()
            base = f"http://127.0.0.1:{server.server_port}"
            page.goto(base + "/c/shared")
            # A later test reuses the context without another navigation.
            collector.node = "later-test"
            collector.session(base + "/c/unrelated")
            page.get_by_label("Input").fill("actual input")
            page.route("**/v1/fake", lambda route: route.fulfill(json={"stand_in": True}))
            page.evaluate("fetch('/v1/fake').then(r => r.json())")
            replacement = tmp_path / "replacement.json"
            replacement.write_text('{"from_file": true}')
            page.route("**/v1/from-file", lambda route: route.fulfill(path=str(replacement)))
            assert page.evaluate("fetch('/v1/from-file').then(r => r.json())") == {
                "from_file": True
            }
            page.route("**/v1/from-response", lambda route: route.fulfill(response=route.fetch()))
            page.evaluate("fetch('/v1/from-response').then(r => r.json())")
            port = sockets.socket.getsockname()[1]
            result = exchange(page, f"ws://127.0.0.1:{port}/terminal")
            assert result == "observed-output"
            final_items.append({"id": "final-turn", "text": result})
        shutil.rmtree(tmp_path / "temporary-video")
        saved = events(tmp_path / "saved")
        assert any(
            e["kind"] == "browser_fulfill" and e["json"] == {"stand_in": True} for e in saved
        )
        assert any(e["kind"] == "browser_route_registered" for e in saved)
        snapshots = [e for e in saved if e["kind"] == "session_items"]
        assert len(snapshots) == 1
        assert snapshots[0]["session_id"] == "shared"
        assert snapshots[0]["test_id"] == "later-test"
        assert snapshots[0]["body"]["data"] == final_items
        fulfilled = [e for e in saved if e["kind"] == "browser_fulfill"]
        assert any(e["path"] == str(replacement) and e["source_sha256"] for e in fulfilled)
        assert any(
            e["response_source"] and e["response_source"]["status"] == 200 for e in fulfilled
        )
        frames = [e for e in saved if e["kind"] == "websocket_frame"]
        assert {(e["direction"], e["payload"]) for e in frames} == {
            ("framesent", "native-input"),
            ("framereceived", "observed-output"),
        }
        context_ids = {e["context_id"] for e in frames}
        assert len(context_ids) == 1
        assert all(
            e["context_id"] in context_ids
            for e in saved
            if e["kind"] in {"browser_response", "browser_fulfill"}
        )
        assert not [e for e in saved if e["kind"] == "collection_error"]
        assert list((tmp_path / "saved").glob("video-*.webm"))
        trace = next((tmp_path / "saved").glob("trace-*.zip"))
        with zipfile.ZipFile(trace) as archive:
            content = b"\n".join(
                archive.read(n) for n in archive.namelist() if n.endswith(".trace")
            )
        assert b"actual input" in content
    finally:
        collector.patch.undo()
        sockets.shutdown()
        socket_thread.join(timeout=3)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("mode", ["close", "silent", "mismatch"])
def test_websocket_exchange_finishes_when_server_does_not_reply_as_expected(mode, browser):
    import contextlib
    import threading

    from playwright.sync_api import Error
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.server import serve

    def handle(socket):
        socket.recv(timeout=5)
        if mode == "mismatch":
            socket.send("unexpected:wrong-input")
        elif mode == "silent":
            with contextlib.suppress(ConnectionClosed):
                socket.recv(timeout=5)

    server = serve(handle, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with browser.new_context() as context:
            page = context.new_page()
            url = f"ws://127.0.0.1:{server.socket.getsockname()[1]}/terminal"
            if mode == "mismatch":
                assert exchange(page, url) == "unexpected:wrong-input"
            else:
                expected = "closed before response" if mode == "close" else "timed out"
                with pytest.raises(Error, match=expected):
                    exchange(page, url, timeout=1000)
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_driver_can_take_over_tracing(tmp_path, browser):
    collector = Evidence(tmp_path / "saved")
    try:
        collector.install_browser()
        with browser.new_context() as context:
            page = context.new_page()
            page.set_content('<input aria-label="Input">')
            page.get_by_label("Input").fill("before driver trace")
            context.tracing.start(screenshots=True, snapshots=True)
            page.get_by_label("Input").fill("during driver trace")
            context.tracing.stop(path=str(tmp_path / "driver.zip"))
        assert (tmp_path / "driver.zip").is_file()
        saved = events(tmp_path / "saved")
        assert not [e for e in saved if e["kind"] == "collection_error"]
        assert any(e["kind"] == "trace_owner" and e["owner"] == "driver" for e in saved)
        assert len(list((tmp_path / "saved").glob("trace-*.zip"))) == 2
    finally:
        collector.patch.undo()


def test_pytest_trace_and_evidence_can_share_a_context(tmp_path, browser, new_context):
    collector = Evidence(tmp_path / "saved")
    try:
        collector.install_browser()
        # new_context starts pytest-playwright's trace when --tracing=on is set.
        context = new_context()
        page = context.new_page()
        page.set_content("<p>observed</p>")
        assert page.get_by_text("observed").is_visible()
        context.close()
        assert not [e for e in events(tmp_path / "saved") if e["kind"] == "collection_error"]
    finally:
        collector.patch.undo()


def test_raw_trace_never_enters_bundle_when_redaction_fails(tmp_path, browser, monkeypatch):
    from dev.repro_env import pytest_evidence

    collector = Evidence(tmp_path / "saved")
    seen = []

    def fail(path, secrets):
        assert path.is_file()
        assert not path.is_relative_to(collector.directory)
        seen.append(path)
        raise PermissionError("cannot sanitize or remove raw trace")

    monkeypatch.setattr(pytest_evidence, "sanitize_trace", fail)
    try:
        collector.install_browser()
        with browser.new_context() as context:
            context.new_page().set_content("<p>observed</p>")
        assert seen
        assert not list(collector.directory.glob("*.zip"))
        saved = events(collector.directory)
        assert any(
            e["kind"] == "collection_error" and e["operation"] == "trace_stop" for e in saved
        )
        assert not any(
            e["kind"] == "artifact" and e.get("kind_of_artifact") == "playwright_trace"
            for e in saved
        )
    finally:
        collector.patch.undo()


@pytest.mark.parametrize("tracing", ["off", "on", "retain-on-failure"])
@pytest.mark.parametrize("fails", [False, True, "abrupt"])
def test_pytest_playwright_teardown_retains_trace(tmp_path, tracing, fails):
    import json
    import os
    import sys
    import zipfile

    from dev.repro_env.execution import run

    (tmp_path / "execution-context.json").write_text("{}")
    source = tmp_path / "test_browser.py"
    source.write_text(
        "def test_browser(page):\n"
        "    page.set_content('<input aria-label=\"Input\">')\n"
        '    page.get_by_label("Input").fill("retained browser action")\n'
        + ("    import os; os._exit(7)\n" if fails == "abrupt" else f"    assert {not fails}\n")
    )
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_PLUGINS": ""}
    code = run(
        tmp_path,
        [
            sys.executable,
            "-m",
            "pytest",
            str(source),
            "-p",
            "pytest_playwright.pytest_playwright",
            "-p",
            "pytest_base_url.plugin",
            "-o",
            "addopts=",
            "--confcutdir",
            str(tmp_path),
            f"--tracing={tracing}",
            "--output",
            str(tmp_path / "plugin-output"),
            "-q",
        ],
        env,
    )
    assert code == (7 if fails == "abrupt" else int(fails))
    attempt = next((tmp_path / "execution").glob("*/attempt.json"))
    record = json.loads(attempt.read_text())
    if fails == "abrupt":
        assert record["artifacts_complete"] and not record["capture_complete"]
        assert any(
            e.get("error_type") == "CollectorInterrupted" for e in record["collection_errors"]
        )
        assert list(attempt.parent.glob("source-*.py"))
        return
    assert record["capture_complete"] and not record["collection_errors"]
    traces = list(attempt.parent.glob("trace-*.zip"))
    assert traces
    content = b""
    for trace in traces:
        with zipfile.ZipFile(trace) as archive:
            content += b"\n".join(
                archive.read(n) for n in archive.namelist() if n.endswith(".trace")
            )
    assert b"retained browser action" in content
    assert list(attempt.parent.glob("screen-*.png"))
    if tracing == "on" or (tracing == "retain-on-failure" and fails):
        assert list((tmp_path / "plugin-output").glob("**/trace.zip"))


def test_discarded_trace_storage_failure_preserves_caller_stop(tmp_path, browser, monkeypatch):
    from dev.repro_env import pytest_evidence

    collector = Evidence(tmp_path / "saved")
    try:
        collector.install_browser()
        with browser.new_context() as context:
            context.new_page().set_content("<p>observed</p>")

            def fail(**kwargs):
                raise OSError("temporary storage unavailable")

            monkeypatch.setattr(pytest_evidence.tempfile, "TemporaryDirectory", fail)
            context.tracing.stop()
        saved = events(collector.directory)
        assert any(
            e["kind"] == "collection_error" and e["operation"] == "trace_prepare" for e in saved
        )
        assert list(collector.directory.glob("screen-*.png"))
    finally:
        collector.patch.undo()


def test_unstopped_caller_trace_reports_incomplete_capture(tmp_path, browser):
    collector = Evidence(tmp_path / "saved")
    try:
        collector.install_browser()
        with browser.new_context() as context:
            context.tracing.start(screenshots=True, snapshots=True)
            context.new_page().set_content("<p>caller trace</p>")
        saved = events(collector.directory)
        assert any(
            e["kind"] == "collection_incomplete" and e["operation"] == "trace_stop" for e in saved
        )
        assert list(collector.directory.glob("screen-*.png"))
    finally:
        collector.patch.undo()


@pytest.mark.parametrize("with_path", [False, True])
def test_caller_stop_exception_is_preserved_with_optional_chunk_capture(
    tmp_path, browser, monkeypatch, with_path
):
    with browser.new_context() as probe:
        tracing_type = type(probe.tracing)
    original = tracing_type.stop
    fail_stop = True

    def stop(tracing, *, path=None):
        if fail_stop:
            raise RuntimeError("caller stop failed")
        return original(tracing, path=path)

    monkeypatch.setattr(tracing_type, "stop", stop)
    collector = Evidence(tmp_path / "saved")
    collector.node = "caller-stop-test"
    try:
        collector.install_browser()
        with browser.new_context() as context:
            try:
                context.new_page().set_content("<p>observed</p>")
                with pytest.raises(RuntimeError, match="caller stop failed"):
                    context.tracing.stop(path=tmp_path / "caller.zip" if with_path else None)
                assert collector.contexts[context]["trace_active"]
            finally:
                fail_stop = False
        errors = [
            e
            for e in collector.journal.errors
            if e["operation"] == "trace_stop" and e.get("detail") == "caller stop failed"
        ]
        assert len(errors) == 1
        assert errors[0]["test_id"] == "caller-stop-test"
    finally:
        collector.patch.undo()


def test_collector_close_records_native_stop_failure_once(tmp_path, browser, monkeypatch):
    with browser.new_context() as probe:
        tracing_type = type(probe.tracing)

    def fail(tracing, *, path=None):
        raise RuntimeError("native stop failed")

    monkeypatch.setattr(tracing_type, "stop", fail)
    collector = Evidence(tmp_path / "saved")
    try:
        collector.install_browser()
        with browser.new_context() as context:
            context.new_page().set_content("<p>observed</p>")
        errors = [e for e in collector.journal.errors if e.get("detail") == "native stop failed"]
        assert len(errors) == 1
        assert errors[0]["phase"] == "before_browser_close"
        saved_errors = [
            e
            for e in events(collector.directory)
            if e["kind"] == "collection_error" and e.get("detail") == "native stop failed"
        ]
        assert len(saved_errors) == 1
        assert context not in collector.contexts
        assert list(collector.directory.glob("trace-*.zip"))
        assert list(collector.directory.glob("screen-*.png"))
    finally:
        collector.patch.undo()


@pytest.mark.parametrize("caller_stop", [False, True])
def test_optional_chunk_failure_does_not_change_caller_stop(
    tmp_path, browser, monkeypatch, caller_stop
):
    collector = Evidence(tmp_path / "saved")
    try:
        collector.install_browser()
        with browser.new_context() as context:
            context.new_page().set_content("<p>observed</p>")

            def fail(*, path=None):
                raise OSError("chunk storage unavailable")

            monkeypatch.setattr(context.tracing, "stop_chunk", fail)
            if caller_stop:
                context.tracing.stop()
                assert not collector.contexts[context]["trace_active"]
        errors = [
            e
            for e in collector.journal.errors
            if e["operation"] == "trace_stop" and e.get("detail") == "chunk storage unavailable"
        ]
        assert len(errors) == 1
        assert errors[0]["phase"] == ("caller_stop" if caller_stop else "before_browser_close")
        assert list(collector.directory.glob("screen-*.png"))
    finally:
        collector.patch.undo()
