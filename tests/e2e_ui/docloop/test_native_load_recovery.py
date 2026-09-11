"""Browser lifecycle checks using the real surface and a synthetic same-origin Lab.

No host, credentials, kernels, or model calls. Requires installed web dependencies
and Node 22+. The iframe fixture deliberately stalls/restores/rejects Lab startup.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

_ROOT = Path(__file__).resolve().parents[3]
_HTML = """<!doctype html><html><head>
<link rel="stylesheet" href="/src/components/docloop/docloop-chat-layout.css">
</head><body><div id="root"></div>
<script type="module">
import RefreshRuntime from '/@react-refresh';
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => (type) => type;
window.__vite_plugin_react_preamble_installed__ = true;
const {default: React} = await import('/node_modules/.vite/deps/react.js');
const {default: {createRoot}} = await import('/node_modules/.vite/deps/react-dom_client.js');
const {NativeNotebookSurface} = await import('/src/components/docloop/NativeNotebookSurface.tsx');
const root = createRoot(document.getElementById('root'));
window.showSurface = (active = true) => root.render(
  React.createElement(NativeNotebookSurface, {sessionId:'fixture', active}));
window.showSurface();
</script></body></html>"""
_LAB = """<!doctype html><html><body>Local notebook fixture<script>
window.jupyterapp = {
  restored: READY ? Promise.resolve() : new Promise((resolve, reject) => {
    window.finishRestore = resolve; window.failRestore = reject;
  }),
  shell: {mode: 'multiple-document', collapseLeft() {}, collapseRight() {},
    currentWidget: {sessionContext: {kernelDisplayStatus: 'idle'}}}
};
</script></body></html>"""


@pytest.fixture
def surface_url(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "OMNIGENT_URL": "http://127.0.0.1:1"}
    with (tmp_path / "vite.log").open("w") as log:
        process = subprocess.Popen(
            [
                "node",
                "node_modules/vite/bin/vite.js",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--strictPort",
            ],
            cwd=_ROOT / "web",
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(200):
                if process.poll() is not None:
                    pytest.fail((tmp_path / "vite.log").read_text())
                try:
                    if httpx.get(url + "/@vite/client").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("Local Vite fixture did not start")
            yield url
        finally:
            process.terminate()
            process.wait(timeout=15)


@pytest.mark.parametrize("failure", ["timeout", "rejection"])
def test_native_load_recovery(surface_url, failure, tmp_path):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        loads = []
        descriptors = []

        def api(route):
            path = route.request.url.split("/docloop", 1)[1]
            if path == "/document":
                route.fulfill(json={"format": "ipynb"})
            elif path == "/jupyter":
                descriptors.append(path)
                route.fulfill(
                    json={"url": "/v1/sessions/fixture/docloop/jupyter/lab/tree/task.ipynb"}
                )
            elif path.startswith("/jupyter/lab/tree/"):
                loads.append(path)
                route.fulfill(
                    content_type="text/html",
                    body=_LAB.replace("READY", "true" if len(loads) > 1 else "false"),
                )
            elif path.startswith("/versions"):
                route.fulfill(
                    json={
                        "schema_version": 1,
                        "session_id": "fixture",
                        "binding_id": "b" * 64,
                        "versions": [],
                    }
                )
            else:
                route.fulfill(status=404)

        page.route("**/v1/**", api)
        page.route(
            "**/surface-fixture", lambda route: route.fulfill(content_type="text/html", body=_HTML)
        )
        page.clock.install()
        page.goto(surface_url + "/surface-fixture")
        frame = page.frame_locator("iframe")
        expect(frame.locator("body")).to_contain_text("Local notebook fixture")
        # Let the parent attach its restoration promise observer first.
        page.clock.run_for(500)
        if failure == "timeout":
            page.clock.fast_forward(120_000)
            expect(page.get_by_role("alert")).to_contain_text("did not finish loading")
        else:
            frame.locator("body").evaluate(
                '() => window.failRestore(new Error("fixture failure"))'
            )
            expect(page.get_by_role("alert")).to_contain_text("could not restore")
        evidence = Path(os.environ.get("OMNIGENT_E2E_EVIDENCE_DIR", tmp_path))
        evidence.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(evidence / f"native-load-{failure}.png"), full_page=True)
        page.clock.fast_forward(120_000)
        assert len(loads) == len(descriptors) == 1
        page.get_by_role("button", name="Try again").click()
        expect(frame.locator("body")).to_contain_text("Local notebook fixture")
        page.clock.run_for(750)
        expect(page.get_by_role("status")).to_have_text("Kernel: idle")
        page.evaluate('window.savedFrame = document.querySelector("iframe")')
        page.evaluate("window.showSurface(false)")
        page.evaluate("window.showSurface(true)")
        page.get_by_role("button", name="History", exact=True).click()
        page.get_by_role("button", name="JupyterLab", exact=True).click()
        page.clock.fast_forward(240_000)
        assert page.evaluate('window.savedFrame === document.querySelector("iframe")')
        assert len(loads) == len(descriptors) == 2
        expect(page.get_by_role("alert")).to_have_count(0)
        assert errors == []
        browser.close()
