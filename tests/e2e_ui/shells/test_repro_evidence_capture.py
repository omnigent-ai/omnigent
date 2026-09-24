"""Exercise evidence collection with a real browser and local test server."""

from dev.repro_env.pytest_evidence import Evidence
from tests.test_repro_execution import events


def test_browser_trace_preserves_actions_mock_boundary_and_video(tmp_path):
    import shutil
    import threading
    import zipfile
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from playwright.sync_api import sync_playwright
    from websockets.sync.server import serve

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if self.path == "/":
                self.send_header("Content-Type", "text/html")
                body = b'<input aria-label="Input"><button>Observe</button>'
            else:
                self.send_header("Content-Type", "application/json")
                body = b'{"data":[],"has_more":false}'
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def echo(socket):
        assert socket.recv() == "native-input"
        socket.send("observed-output")

    sockets = serve(echo, "127.0.0.1", 0)
    socket_thread = threading.Thread(target=sockets.serve_forever, daemon=True)
    socket_thread.start()
    collector = Evidence(tmp_path / "saved")
    collector.node = "browser-attempt"
    try:
        collector.install_browser()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-sandbox"])
            context = browser.new_context(record_video_dir=str(tmp_path / "temporary-video"))
            page = context.new_page()
            base = f"http://127.0.0.1:{server.server_port}"
            page.goto(base)
            page.get_by_label("Input").fill("actual input")
            page.route("**/v1/fake", lambda route: route.fulfill(json={"stand_in": True}))
            page.evaluate("fetch('/v1/fake').then(r => r.json())")
            port = sockets.socket.getsockname()[1]
            result = page.evaluate(
                """url => new Promise(resolve => {
                const socket = new WebSocket(url);
                socket.onopen = () => socket.send('native-input');
                socket.onmessage = event => { socket.close(); resolve(event.data); };
            })""",
                f"ws://127.0.0.1:{port}/terminal",
            )
            assert result == "observed-output"
            context.close()
            browser.close()
        shutil.rmtree(tmp_path / "temporary-video")
        saved = events(tmp_path / "saved")
        assert any(
            e["kind"] == "browser_fulfill" and e["json"] == {"stand_in": True} for e in saved
        )
        assert any(e["kind"] == "browser_route_registered" for e in saved)
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
