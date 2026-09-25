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

                body = json.dumps({"data": final_items, "has_more": False}).encode()
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
        assert len(list((tmp_path / "saved").glob("trace-*.zip"))) == 1
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
        assert not any(e.get("kind_of_artifact") == "playwright_trace" for e in saved)
    finally:
        collector.patch.undo()
