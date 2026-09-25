"""E2E: a catalog-style generic-ACP agent must run on the model the user picked.

ACP agents advertise model selection two ways: a ``model`` session config option announced via
``config_option_update`` (what ``AcpExecutor._apply_model_override`` drives with
``session/set_config_option``), or a ``models`` catalog returned from
``session/new`` (``availableModels`` + ``currentModelId``) driven with
``session/set_model``. Cline is the second kind: it never sends
``config_option_update``, ignores a non-standard ``model`` field in
``session/new``, and honors only ``session/set_model`` — so the driver must
issue it, applying the entry-level ``model:`` a user wrote on their ``acp:``
config block when the turn carries no per-turn ``/model`` pick. A driver that
skips either half leaves the agent silently running on its own default
(metered) model no matter what the user configured; with real Cline that
burned Cline Credits until every turn returned a bare ``end_turn`` with no
output at all.

These tests drive the real user journey against a live server: register a
generic ACP agent whose ``acp:`` entry carries ``model:``, launch it exactly as
``omnigent run --harness acp:<slug>`` does (the CLI's own launcher
materializer), open the session in the web UI, and send a message. The agent is
a hermetic catalog-style fake speaking the Agent Client Protocol over stdio
(the Cline shape — same fake-agent pattern as
``tests/inner/test_acp_executor.py``):

* ``test_configured_model_reaches_catalog_style_acp_agent`` asserts the reply
  names the configured model. It fails while the driver never issues
  ``session/set_model`` (the agent stays on its metered default) and passes
  once the configured/picked model is delivered through the ``session/new``
  catalog mechanism.
* ``test_web_turn_produces_output_when_agent_defaults_to_metered_model``
  injects the downstream fault the reporter logged: like Cline with an
  exhausted Cline Credits balance, the fake returns a bare
  ``{"stopReason": "end_turn"}`` with no output while it is still on the
  metered default model. The web turn dispatches, the session returns to idle,
  and no assistant output is ever produced — the headline symptom. It passes
  once the model switch lands (the metered path is never taken).
"""

from __future__ import annotations

import gzip
import io
import json
import re
import shlex
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online

_ACP_SLUG = "fake-cline"
# The model the user writes on the acp: config entry (report: a cline-pass/*
# id, which Cline accepts via session/set_model but never enumerates).
_CONFIGURED_MODEL = "cline-pass/deepseek-v4.1-flash"
# The agent's own default — the metered model real Cline fell back to.
_DEFAULT_METERED_MODEL = "anthropic/claude-sonnet-5"
# Marker the fake agent prefixes its reply with so assertions can read which
# model actually served the turn (the user-visible analog of the reporter
# checking Cline's recorded session state).
_MODEL_MARKER = "ACP_MODEL_IN_USE="

# A minimal catalog-style ACP agent (the Cline shape), JSON-RPC 2.0 over
# newline-delimited stdio, mirroring the hermetic fake in
# ``tests/inner/test_acp_executor.py``:
#
# * ``session/new`` returns a ``models`` catalog (``availableModels`` +
#   ``currentModelId``). No ``config_option_update`` is ever sent, and a
#   non-standard ``model`` param in ``session/new`` is ignored — like Cline.
# * ``session/set_model`` is the only switch mechanism it honors, and (like
#   Cline with ``cline-pass/*`` ids) it accepts ids it never enumerated.
# * ``session/set_config_option`` (and anything else) is method-not-found.
# * ``session/prompt`` replies with ``ACP_MODEL_IN_USE=<current model>``.
#   With ``--metered-silent``, while still on the metered default it instead
#   returns a bare ``{"stopReason": "end_turn"}`` with no output — the exact
#   over-ACP behavior the reporter logged from Cline once its Credits balance
#   was exhausted (the real error appears only on Cline's internal hook
#   stream).
#
# Stdlib only, so any Python interpreter on the runner host can run it.
_FAKE_CATALOG_ACP_AGENT = r"""
import sys, json

METERED_SILENT = "--metered-silent" in sys.argv[1:]
DEFAULT_MODEL = "anthropic/claude-sonnet-5"
current_model = DEFAULT_MODEL

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"promptCapabilities": {"image": False}},
        }})
    elif method == "session/new":
        # Catalog-style advertisement: models live in the session/new result.
        # Any non-standard `model` param the client sent is ignored (Cline
        # behavior, verified in the issue).
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": "fake-cline-session-1",
            "models": {
                "availableModels": [
                    {"modelId": DEFAULT_MODEL, "name": "Claude Sonnet (metered)"},
                ],
                "currentModelId": current_model,
            },
        }})
    elif method == "session/set_model":
        requested = (msg.get("params") or {}).get("modelId")
        if isinstance(requested, str) and requested:
            current_model = requested
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32602, "message": "modelId required"}})
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        if METERED_SILENT and current_model == DEFAULT_MODEL:
            # Exhausted-balance Cline over ACP: no update, no error — just a
            # bare stop reason. The turn "completes" with nothing in it.
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
        else:
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": sid, "update": {
                      "sessionUpdate": "agent_message_chunk",
                      "content": {"type": "text",
                                  "text": "ACP_MODEL_IN_USE=" + current_model}}}})
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "stopReason": "end_turn",
                "usage": {"inputTokens": 12, "outputTokens": 6, "totalTokens": 18},
            }})
    elif mid is not None and method is not None:
        # session/set_config_option and anything else: unsupported, like Cline
        # (it advertises no session config options).
        send({"jsonrpc": "2.0", "id": mid,
              "error": {"code": -32601, "message": "Method not found: " + str(method)}})
"""


def _acp_launcher_bundle(agent_command: str) -> bytes:
    """Gzip-tar the launcher YAML ``omnigent run --harness acp:<slug>`` generates.

    Calls the CLI's real materializer with an :class:`AcpAgentEntry` carrying
    the entry-level ``model:`` — byte-for-byte the spec no-AGENT run dispatch
    produces for a user whose ``acp:`` config block names a model (the report's
    config shape).

    :param agent_command: Command line that launches the fake ACP agent.
    :returns: The gzipped tarball bytes for the multipart session create.
    """
    from omnigent.cli import _materialize_harness_launcher_file
    from omnigent.onboarding.acp_auth import AcpAgentEntry

    launcher = _materialize_harness_launcher_file(
        harness=f"acp:{_ACP_SLUG}",
        model=None,
        system_prompt=None,
        acp_agent=AcpAgentEntry(
            slug=_ACP_SLUG,
            name="Fake Cline",
            command=agent_command,
            model=_CONFIGURED_MODEL,
        ),
    )
    data = launcher.read_bytes()
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        info = tarfile.TarInfo(name=launcher.name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def acp_session_factory(
    live_server: str,
    runner_id: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Callable[..., tuple[str, str]]]:
    """Create sessions bound to the generated ``acp:<slug>`` launcher agent.

    Writes the hermetic fake ACP agent script to disk, uploads the launcher
    bundle via the same multipart ``POST /v1/sessions`` the CLI's run dispatch
    uses, and binds each session to the spawned runner.

    :param live_server: Spawned server base URL.
    :param runner_id: Token-bound runner id to bind sessions to.
    :param tmp_path: Per-test dir for the fake agent script.
    :param tmp_path_factory: Temp directories for a replacement runner's logs.
    :returns: A factory ``(*agent_args) -> (base_url, session_id)``.
    """
    agent_script = tmp_path / "fake_catalog_acp_agent.py"
    agent_script.write_text(_FAKE_CATALOG_ACP_AGENT)

    created: list[str] = []
    respawned_runner: subprocess.Popen[bytes] | None = None
    # Earlier tests may deliberately stop the session-scoped runner.
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)

    def _create(*agent_args: str) -> tuple[str, str]:
        command = shlex.join([sys.executable, str(agent_script), *agent_args])
        create_resp = httpx.post(
            f"{live_server}/v1/sessions",
            data={"metadata": json.dumps({})},
            files={"bundle": ("agent.tar.gz", _acp_launcher_bundle(command), "application/gzip")},
            timeout=30.0,
        )
        create_resp.raise_for_status()
        session_id = create_resp.json()["session_id"]
        created.append(session_id)
        patch_resp = httpx.patch(
            f"{live_server}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=10.0,
        )
        patch_resp.raise_for_status()
        return (live_server, session_id)

    try:
        yield _create
    finally:
        try:
            for session_id in created:
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            if respawned_runner is not None:
                respawned_runner.terminate()
                try:
                    respawned_runner.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned_runner.kill()
                    respawned_runner.wait(timeout=5)


def _session_status(base_url: str, session_id: str) -> str:
    """Read the status exposed by the slim session snapshot."""
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return str(response.json()["status"])


def _wait_for_turn_round_trip(base_url: str, session_id: str, timeout_s: float = 120.0) -> None:
    """Wait for the session to leave ``idle`` and settle back to ``idle``.

    Polling for ``idle`` right after a send false-fires on the pre-turn idle,
    so the session must be observed leaving idle first.

    :param base_url: Server base URL.
    :param session_id: The driven session.
    :param timeout_s: Overall deadline for the round trip.
    """
    deadline = time.monotonic() + timeout_s
    left_idle = False
    while time.monotonic() < deadline:
        status = _session_status(base_url, session_id)
        if status != "idle":
            left_idle = True
        elif left_idle:
            return
        time.sleep(0.2)
    pytest.fail(
        f"session {session_id} never completed a turn round-trip "
        f"(leave idle -> return to idle) within {timeout_s:.0f}s"
    )


def test_configured_model_reaches_catalog_style_acp_agent(
    page: Page,
    acp_session_factory: Callable[..., tuple[str, str]],
) -> None:
    """The ``model:`` on an ``acp:`` entry must reach a catalog-style agent.

    Journey: register the agent with ``model: cline-pass/deepseek-v4.1-flash``,
    open its session in the web UI, send a message, and read which model served
    the turn from the agent's reply. Fails while the ACP driver never issues
    ``session/set_model`` (the reply names the agent's metered default model);
    passes when the configured model is delivered to the agent.
    """
    base_url, session_id = acp_session_factory()
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill("Which model are you running on?")
    composer.press("Enter")

    # The fake always replies with its live model — once the marker renders,
    # the turn round-tripped and the model in use is user-visible.
    reply = page.get_by_text(re.compile(re.escape(_MODEL_MARKER))).first
    expect(reply).to_be_visible(timeout=120_000)

    reported = reply.inner_text()
    match = re.search(re.escape(_MODEL_MARKER) + r"(\S+)", reported)
    assert match is not None, f"reply did not carry a model marker: {reported!r}"
    assert match.group(1) == _CONFIGURED_MODEL, (
        f"the agent served the turn on {match.group(1)!r}: the model configured on "
        f"the acp: agent entry ({_CONFIGURED_MODEL!r}) never reached the "
        "catalog-style ACP agent — the driver ignored the session/new models "
        "catalog and never issued session/set_model"
    )


def test_web_turn_produces_output_when_agent_defaults_to_metered_model(
    page: Page,
    acp_session_factory: Callable[..., tuple[str, str]],
) -> None:
    """A web turn must produce assistant output, not a silent idle return.

    Fault-injected downstream of the model bug: the fake agent behaves like
    Cline with an exhausted Credits balance — while left on its metered default
    model, a prompt returns a bare ``end_turn`` with no output. Because the
    driver never switches the agent to the configured (unmetered) model, the
    web UI shows the user message, the session returns to idle, and no
    assistant output is ever produced. Passes once the model switch lands: the
    agent runs on ``cline-pass/*`` and replies normally.
    """
    base_url, session_id = acp_session_factory("--metered-silent")
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill("test")
    composer.press("Enter")

    # The user message persists in the transcript...
    expect(page.get_by_text("test", exact=True).first).to_be_visible(timeout=30_000)

    # ...and the turn dispatches and completes (leave idle -> back to idle).
    _wait_for_turn_round_trip(base_url, session_id)

    # The reported failure point: the session is idle again with only the user
    # message persisted and zero assistant output. With the model correctly
    # switched off the metered default, the agent's reply renders here.
    expect(page.get_by_text(re.compile(re.escape(_MODEL_MARKER))).first).to_be_visible(
        timeout=30_000
    )
