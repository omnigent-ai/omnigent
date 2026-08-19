"""A 200 HTML sign-in page must not be consumed as a JSON tool-call response (#4997).

An authenticating edge (reverse proxy / IdP) answering /mcp with a 200
text/html sign-in page used to hit resp.json(), whose SyntaxError message
quotes the body — leaking sign-in document text into a tool result — and
reads as an unactionable parse error instead of an auth failure.
"""

from __future__ import annotations

from pathlib import Path

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pi_native_extension import _extension_path, _run_node  # shared harness helpers

_HARNESS = r"""
const assert = require("assert").strict;
const path = require("path");
const fs = require("fs");

const extensionPath = process.argv[1];
const configPath = path.join(require("os").tmpdir(), `pi-signin-${process.pid}.json`);
fs.writeFileSync(configPath, JSON.stringify({
  serverUrl: "http://omnigent.test",
  sessionId: "session-1",
  authHeaders: { authorization: "Bearer test" },
  tools: [{ name: "omnigent_search", description: "search", parameters: { type: "object" } }],
}));
process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

// The authenticating-edge shape: 200, text/html, a sign-in document with
// values a tool result must never quote.
const SIGN_IN_HTML = "<!DOCTYPE html><html><head><title>Sign in</title>" +
  "<meta name=\"csrf\" content=\"SECRET-CSRF\"></head><body>state=SECRET</body></html>";

let mcpCalls = 0;
global.fetch = async (_url, request) => {
  const body = JSON.parse(request.body);
  if (body.method === "tools/call") {
    mcpCalls += 1;
    return {
      ok: true,
      status: 200,
      headers: { get: (name) => (name === "content-type" ? "text/html; charset=utf-8" : null) },
      json: async () => { throw new SyntaxError("Unexpected token '<', \"" + SIGN_IN_HTML.slice(0, 40) + "\"... is not valid JSON"); },
      text: async () => SIGN_IN_HTML,
    };
  }
  return { ok: true, headers: { get: () => "application/json" }, json: async () => ({}) };
};
global.setInterval = () => ({ fakeInterval: true });

const registered = {};
const handlers = {};
const pi = {
  registerCommand() {},
  registerTool(tool) { registered[tool.name] = tool; },
  on(eventName, handler) { handlers[eventName] = handler; },
};
require(extensionPath)(pi);
const ctx = { ui: { setTitle() {}, setStatus() {}, notify() {} }, sessionManager: {} };

(async () => {
  await handlers["session_start"](ctx, ctx);
  const tool = registered["omnigent_search"];
  assert.ok(tool, "extension registered the Omnigent tool");

  const result = await tool.execute("call-1", { q: "hi" });
  const payload = JSON.stringify(result);

  assert.ok(mcpCalls === 1, "the tool call was dispatched exactly once");
  assert.ok(!payload.includes("SECRET"), "no sign-in body text may reach the tool result: " + payload);
  assert.ok(!payload.includes("<!DOCTYPE"), "no HTML may reach the tool result");
  assert.ok(/non-JSON|authenticat/i.test(payload), "must be classified as auth/edge failure: " + payload);
  assert.ok(result.isError === true, "must be an isError tool result");
  console.log("OK");
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_html_signin_200_is_auth_failure_not_parse_error():
    result = _run_node(_HARNESS, str(_extension_path()), "/tmp")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
