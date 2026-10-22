"""pi-native: a 200 text/html sign-in page is consumed as a JSON tool-call result.

The journey
-----------
A user runs a pi-native session (``omnigent pi``) on a topology with an
authenticating edge (a reverse proxy or an IdP) in front of the Omnigent
server. The Pi agent calls a bridged Omnigent tool, which the extension
dispatches by POSTing a JSON-RPC ``tools/call`` to
``POST /v1/sessions/{id}/mcp``. When the caller's auth has lapsed, the proxy
answers that route with an HTTP ``200`` **sign-in document** (``text/html``)
instead of the runner's JSON envelope -- a common proxy behaviour, not a
Databricks-specific one.

Observed today (the bug)
------------------------
``postMcpToolsCall`` in
``omnigent/resources/pi_native/omnigent_pi_native_extension.js`` checks only
``resp.ok`` and then calls ``resp.json()``. A ``200 text/html`` satisfies
``resp.ok``, so ``resp.json()`` throws a ``SyntaxError`` whose message quotes a
prefix of the response body. The throw is caught by the enclosing ``catch``,
which returns the raw error message verbatim as the tool result:

    Omnigent tool call failed: Unexpected token '<', "<!DOCTYPE "... is not valid JSON

Two defects, both reproduced by this test on the current build:

1. **Body text leaks into the tool result.** ``<!DOCTYPE`` (and, with a
   differently-ordered document, the sign-in page's CSRF/``state``/tenant
   values) reaches the tool result the model receives. A sign-in document's
   body must never reach a tool result.
2. **The failure is unactionable.** "not valid JSON" tells neither the user nor
   the agent that the remedy is to re-authenticate.

Expected / the fail -> pass contract
------------------------------------
A JSON consumer at that boundary must accept a response only when status,
declared content type, and parseability all agree. A ``200`` whose content type
is not JSON must be classified as an authentication / sign-in failure and
returned as a bounded ``isError`` tool result that names no body, header, or
URL. This test asserts that expected contract, so it **fails on the current
build** (the tool result quotes the body -- ``<!DOCTYPE`` -- and names no
authentication classification) and **passes after the fix**.

Fidelity
--------
This drives the REAL shipped extension: it is generated exactly as the runner
generates it (``omnigent.harnesses.pi_native.bridge.write_extension_files``,
with a bridged tool in ``config.tools``) and loaded under Node the way Pi loads
it. Only the network boundary is faulted -- ``globalThis.fetch`` answers the
``/mcp`` route with the reported ``200 text/html`` sign-in page (the reported
trigger). The Pi agent's own entry point is exercised: the model-invoked
``pi.registerTool({... execute})`` callback the bridge registers for each
Omnigent tool.

Usage::

    python -m pytest tests/e2e/test_pi_native_html_signin_tool_call_e2e.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from omnigent.harnesses.pi_native import bridge as pi_native_bridge

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required to execute the pi-native extension",
)

# tests/e2e/<this file> -> parents[2] is the worktree root; the Node subprocess
# runs from there so it resolves the same checkout's extension file.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# A registered Omnigent tool the extension will expose via pi.registerTool. Its
# execute() round-trips through POST /v1/sessions/{id}/mcp -- the boundary the
# bug lives at.
_TOOL_NAME = "sys_os_shell"

# Secret markers seeded into the sign-in document. A body-quoting error message
# would surface them; a correct classification quotes nothing.
_CSRF_SECRET = "CSRF-SECRET-abc123"
_STATE_SECRET = "STATE-SECRET-tenant-acme-xyz"

# The proxy/IdP sign-in page returned with HTTP 200 + text/html. Leading
# ``<!DOCTYPE`` is always within even a short SyntaxError body-prefix window, so
# a leak is detectable regardless of the undici/V8 prefix length.
_SIGNIN_HTML = (
    "<!DOCTYPE html><html><head><title>Sign in</title>"
    f'<meta name="csrf-token" content="{_CSRF_SECRET}">'
    '</head><body><form action="/login" method="post">'
    f'<input type="hidden" name="state" value="{_STATE_SECRET}">'
    "<button>Sign in</button></form>Please sign in to continue.</body></html>"
)

# Body / HTML markers that must never appear in a tool result handed to the
# model.
_BODY_MARKERS = ("<!DOCTYPE", "<html", "<head", "<form", "<input", _CSRF_SECRET, _STATE_SECRET)


def _prepare_bridge(tmp_path: Path) -> tuple[Path, Path]:
    """Generate the real pi-native extension + config, with a bridged tool.

    Uses the same helper the runner uses for native Pi sessions, so the test
    covers the generated config, the shipped extension source, and the
    ``pi.registerTool`` execute path together.

    :returns: ``(extension_path, config_path)``.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    extension_path, config_path = pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_html_signin_e2e",
        server_url="https://omnigent.example.internal",
        conversation_url="https://omnigent.example.internal/c/conv_html_signin_e2e",
        auth_headers={"authorization": "Bearer test-token"},
        tools=[
            {
                "name": _TOOL_NAME,
                "description": "Run a shell command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                },
            }
        ],
    )
    return extension_path, config_path


def _drive_tool_call(tmp_path: Path, *, extension_path: Path, config_path: Path) -> dict:
    """Load the real extension under Node, fault the /mcp fetch, dispatch a tool.

    The Node scenario stubs ``globalThis.fetch`` so the ``/mcp`` route answers a
    ``200 text/html`` sign-in page (the reported trigger), mocks only the Pi
    event bus enough to register tools, then invokes the bridge-registered
    tool's ``execute`` -- exactly what Pi does when the model calls the tool.

    :returns: The Pi tool result object the model would receive.
    """
    script = tmp_path / "drive_html_signin.mjs"
    script.write_text(
        textwrap.dedent(
            r"""
            import { createRequire } from "module";
            import fs from "fs";

            const require = createRequire(import.meta.url);
            const extensionPath = process.env.PI_NATIVE_EXTENSION_PATH;
            const toolName = process.env.PI_NATIVE_TOOL_NAME;
            const signinHtml = process.env.PI_NATIVE_SIGNIN_HTML;

            // The authenticating edge answers /mcp with a 200 text/html sign-in
            // page. Every other route returns benign JSON so an unrelated call
            // can't pollute the observation.
            globalThis.fetch = async (url) => {
              if (String(url).endsWith("/mcp")) {
                return new Response(signinHtml, {
                  status: 200,
                  headers: { "content-type": "text/html; charset=utf-8" },
                });
              }
              return new Response("{}", {
                status: 200,
                headers: { "content-type": "application/json" },
              });
            };

            const registered = new Map();
            const pi = {
              on() {},
              registerCommand() {},
              sendUserMessage() {},
              registerTool(spec) {
                if (spec && spec.name) registered.set(spec.name, spec);
              },
            };

            require(extensionPath)(pi);

            const tool = registered.get(toolName);
            if (!tool || typeof tool.execute !== "function") {
              console.error(`tool ${toolName} was not registered`);
              process.exit(3);
            }

            const result = await tool.execute("call-1", { command: "echo hi" });
            process.stdout.write(JSON.stringify(result));
            """
        ),
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "PI_NATIVE_EXTENSION_PATH": str(extension_path),
        "OMNIGENT_PI_NATIVE_CONFIG": str(config_path),
        "PI_NATIVE_TOOL_NAME": _TOOL_NAME,
        "PI_NATIVE_SIGNIN_HTML": _SIGNIN_HTML,
    }
    proc = subprocess.run(
        ["node", str(script)],
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"node scenario failed: rc={proc.returncode}\n{proc.stderr}"
    return json.loads(proc.stdout)


def _result_text(result: dict) -> str:
    """Join the text of every content block in a Pi tool result."""
    content = result.get("content")
    if not isinstance(content, list):
        return json.dumps(result)
    parts = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def test_html_signin_at_mcp_boundary_is_a_bounded_auth_error(tmp_path: Path) -> None:
    """A 200 text/html sign-in page must become a bounded auth error, not a
    body-quoting parse error.

    Journey: bridged tool ``execute`` -> POST /mcp -> proxy answers 200
    text/html sign-in page -> the model receives the tool result.

    Durable contract (fails on the current build, passes after the fix):

    * the tool result quotes NO response body -- none of ``<!DOCTYPE`` /
      ``<html`` / ``<form`` / the CSRF / ``state`` secrets appear (defect 1);
    * the tool result classifies the failure as authentication / sign-in and
      names re-authentication as the remedy (defect 2);
    * it stays ``isError: true`` (the fail-safe the bridge already provides).
    """
    extension_path, config_path = _prepare_bridge(tmp_path)
    result = _drive_tool_call(tmp_path, extension_path=extension_path, config_path=config_path)

    text = _result_text(result)

    # Fail-safe preserved: a bad edge response is still surfaced as an error,
    # never swallowed into a fake success.
    assert result.get("isError") is True, (
        "a 200 text/html sign-in page at the /mcp boundary must still surface as "
        f"an error tool result (isError:true); got: {result!r}"
    )

    # Defect 1 -- no body text may leak into the tool result.
    leaked = [marker for marker in _BODY_MARKERS if marker in text]
    assert not leaked, (
        "the sign-in document's body text leaked into the tool result the model "
        f"receives: {leaked!r}. The bridge treated a 200 text/html response as "
        "JSON, and the SyntaxError from resp.json() quoted the response body. A "
        "sign-in page carries CSRF/state/tenant values, so no body, header, or "
        f"URL text may appear in a tool result.\n  tool result text: {text!r}"
    )

    # Defect 2 -- the failure must be actionable: an authentication / sign-in
    # classification pointing at re-authentication, not a raw 'not valid JSON'.
    lowered = text.lower()
    classifies_auth = any(
        token in lowered
        for token in ("authenticat", "sign in", "sign-in", "signin", "log in", "login")
    )
    assert classifies_auth, (
        "the tool result does not classify the failure as an authentication / "
        "sign-in problem, so neither the user nor the agent is told the remedy "
        f"(re-authenticate). A raw JSON parse error is unactionable.\n"
        f"  tool result text: {text!r}"
    )
    assert "is not valid json" not in lowered, (
        "the tool result is a raw JSON parse error ('is not valid JSON') instead "
        "of an authentication/edge-failure classification.\n"
        f"  tool result text: {text!r}"
    )
