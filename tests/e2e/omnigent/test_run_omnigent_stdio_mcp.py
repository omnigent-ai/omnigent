"""Live E2E: ``omnigent run`` calls a stdio MCP tool.

Proves the full path landed by the stdio-MCP work:

    YAML  →  omnigent Omnigent translator
          (emits ``MCPServerConfig(transport=\"stdio\", ...)``)
          →  ``ToolManager.start()`` (spawns the MCP subprocess
          via ``mcp.client.stdio.stdio_client`` under the shared
          ``wrap_with_srt`` helper)
          →  openai-agents harness sees the MCP tool
          →  LLM (OpenAI ``gpt-4o-mini``) calls the tool
          →  ``echo: <probe>`` string round-trips back through
          stdio, through the harness, through the Omnigent mode reply
          render, into this test's stdout.

No mocks in the whole chain. The MCP server is
``tests/unit/tools/fixtures/echo_stdio_mcp_server.py`` (a FastMCP
subprocess this test spawns at run time); the LLM is OpenAI's
real ``gpt-4o-mini`` endpoint reached via ``$OPENAI_API_KEY``.

**What breaks if this fails:**

- The Omnigent translator silently drops the
  :class:`~omnigent.inner.tools.MCPTool`: the spec loads, but
  the omnigent runtime never registers the tool, the LLM
  never calls it, and the ``echo: <probe>`` fingerprint is
  absent. The unit test
  (``test_omnigent_adapter.py::test_load_mcp_stdio_yaml_
  translates_to_mcp_server``) asserts the translator emits the
  right ``MCPServerConfig``, but doesn't exercise the
  subprocess side.
- ``McpServerConnection._open_stdio_transport`` regresses so
  the subprocess never spawns or the stdio handshake hangs —
  the unit tests patch ``stdio_client``, only this test runs
  the real one against a real FastMCP server.
- ``wrap_with_srt`` mis-wraps on this host's configuration
  (srt present but a runtime path rule denies the subprocess
  from exec'ing ``python``): the unit tests cover the wrap's
  branching truth table but can't catch host-environment
  interactions.

The test is skipped cleanly when ``OPENAI_API_KEY`` is absent
so CI / other developers aren't blocked.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

# Model chosen for predictable tool-calling behavior + low cost.
# gpt-4o-mini follows the "call the tool" instruction reliably
# enough that a single shot produces the echo fingerprint.
_OPENAI_MODEL = "gpt-4o-mini"

# Harness that honors ``OPENAI_API_KEY`` directly (no Databricks
# ``.databrickscfg`` patching needed).
_HARNESS = "openai-agents"

# Probe token the LLM must pass to the ``echo`` tool. Picked to
# be obviously synthetic — a model "guessing" a reply string
# wouldn't match this by accident.
_PROBE = "ap-stdio-mcp-probe-7431"

# Assistant reply MUST include this exact fragment for the test
# to pass. The echo MCP server returns ``f"echo: {text}"`` (see
# ``tests/unit/tools/fixtures/echo_stdio_mcp_server.py``); the LLM
# should either pass that back verbatim or paraphrase around it.
# Either way, ``echo: <probe>`` proves the tool body ran inside
# the MCP subprocess and its output flowed back through the
# full Omnigent mode stack.
_SUCCESS_MARKER = f"echo: {_PROBE}"

# 3 minutes leaves headroom for cold-start imports, subprocess
# spawn, MCP handshake, one LLM round-trip, and shutdown. The
# existing Omnigent mode example-agent tests use 240s; shorter here
# because the MCP is trivial and there are no skill-loading or
# sub-agent phases.
_TIMEOUT_SEC = 180

# Echo MCP fixture path, resolved against the repo root at
# runtime so the test doesn't assume a cwd.
_ECHO_MCP_REL = Path("tests/unit/tools/fixtures/echo_stdio_mcp_server.py")


def _skip_without_openai_key() -> str:
    """
    Skip this test when ``$OPENAI_API_KEY`` is not set to an
    OpenAI key.

    Keeps the suite runnable on CI / in environments where the
    contributor hasn't exported a key. A stale Databricks PAT
    export (``dapi...``) is also skipped — the openai-agents
    harness would hit api.openai.com with it and 401.

    :returns: The OpenAI API key string.
    """
    key = os.environ.get("OPENAI_API_KEY")
    if key is None or not key.strip():
        pytest.skip(
            "OPENAI_API_KEY not set — this test spawns a live "
            "OpenAI turn. Export a real OpenAI key "
            "(``export OPENAI_API_KEY=sk-...``) and rerun."
        )
    key = key.strip()
    if not key.startswith("sk-"):
        pytest.skip(
            f"OPENAI_API_KEY is set but doesn't look like an "
            f"OpenAI key (expected a string starting with 'sk-', "
            f"got {key[:6]!r}...). Refusing to run to avoid a "
            f"confusing 401 against api.openai.com — this test "
            f"targets OpenAI directly, not a Databricks gateway.",
        )
    return key


def _write_stdio_mcp_yaml(tmp_path: Path, repo_root: Path) -> Path:
    """
    Materialize an omnigent agent YAML that declares the
    echo-test MCP as a stdio subprocess.

    The YAML is emitted fresh each test run (rather than checked
    in under ``examples/``) because the ``command:`` is the
    current Python interpreter's absolute path — baking that in
    as a committed example would be either brittle (wrong on
    other machines) or indirect (an ``sh -c`` wrapper to find
    python). A test-generated YAML sidesteps both.

    :param tmp_path: Per-test temporary directory.
    :param repo_root: Omnigent repo root — used to resolve the
        absolute path to the echo MCP fixture.
    :returns: Path to the generated YAML.
    """
    import sys

    echo_server_abs = repo_root / _ECHO_MCP_REL
    assert echo_server_abs.is_file(), (
        f"Expected echo MCP fixture at {echo_server_abs} — the "
        f"test depends on the bundled FastMCP server. If the "
        f"fixture moved, update ``_ECHO_MCP_REL`` alongside."
    )
    agent = {
        "name": "echo_omnigent_stdio_mcp_test",
        "prompt": (
            "You have exactly one tool available: ``echo``, which "
            "takes a single ``text`` argument and returns the "
            'input prefixed with ``"echo: "``. When the user '
            "asks you to echo a specific string, call ``echo`` "
            "with that string as the ``text`` argument, then "
            "reply to the user quoting the tool's exact return "
            "value."
        ),
        "tools": {
            "echo_mcp": {
                "type": "mcp",
                "command": sys.executable,
                "args": [str(echo_server_abs)],
            },
        },
    }
    yaml_path = tmp_path / "echo_omnigent_stdio.yaml"
    yaml_path.write_text(yaml.dump(agent))
    return yaml_path


def test_omnigent_stdio_mcp_tool_roundtrip(tmp_path: Path) -> None:
    """
    Run ``omnigent run <yaml>`` with a stdio MCP; the
    LLM must invoke the tool and the echoed string must appear
    in the agent's final reply.

    Full-stack verification. Other tests cover each layer in
    isolation (translator unit test, runtime transport unit
    tests, ToolManager integration test) — this one catches
    regressions that only surface when every layer runs
    together with a live LLM.

    :param tmp_path: Per-test temporary directory. Used to
        materialize the generated agent YAML so the test owns
        its own copy — no collateral risk to example YAMLs.
    """
    openai_key = _skip_without_openai_key()

    # Resolve repo root from this test file's location. The
    # ``omnigent_repo_root`` session fixture in conftest.py
    # anchors on a worktree-specific constant; this test
    # reproduces the same logic locally to stay independent of
    # the fixture (so it works in any worktree layout).
    repo_root = Path(__file__).resolve().parents[3]
    # Reuse the pytest interpreter for the subprocess so we get the
    # same ``openai-agents`` / ``omnigent`` install the test is
    # running under. The in-tree ``.venv`` isn't guaranteed to
    # have the harness SDK installed (some worktrees skip it), so
    # hardcoding ``repo_root / ".venv" / "bin" / "python"`` would
    # fail with ``ImportError: OpenAIAgentsSDKExecutor requires
    # the 'openai-agents' package`` on those boxes.
    import sys as _sys

    python = Path(_sys.executable)

    yaml_path = _write_stdio_mcp_yaml(tmp_path, repo_root)

    env = dict(os.environ)
    env["OPENAI_API_KEY"] = openai_key
    # Belt-and-suspenders: clear any stale base-URL override
    # that could redirect the openai-agents harness's calls to
    # a Databricks serving endpoint that doesn't know
    # ``gpt-4o-mini``. Without this, a developer who exported
    # ``OPENAI_BASE_URL`` earlier in their shell would see the
    # test mysteriously route to a Databricks gateway.
    env.pop("OPENAI_BASE_URL", None)
    # Ditto for Databricks env vars the Omnigent mode shim might
    # otherwise pick up and use to route credentials.
    for stale in (
        "DATABRICKS_CONFIG_PROFILE",
        "DATABRICKS_TOKEN",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE",
        "CLAUDECODE",
    ):
        env.pop(stale, None)

    # Explicit tool-name hint in the prompt so the LLM doesn't
    # mistake the ask for one of Omnigent' auto-registered
    # builtins (``check_task``, ``sys_cancel_task``, ...). gpt-4o-mini
    # was observed routing to ``check_task`` when the prompt said
    # only "echo X" — it heard "task-id X" instead of "text X".
    prompt = f"Use the ``echo`` tool with text='{_PROBE}' and reply with the tool's exact return."
    args = [
        str(python),
        "-m",
        "omnigent",
        "run",
        str(yaml_path),
        "--model",
        _OPENAI_MODEL,
        "--harness",
        _HARNESS,
        "-p",
        prompt,
    ]
    result = subprocess.run(
        args,
        env=env,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SEC,
    )

    combined = result.stdout + result.stderr

    # Harness-side failure modes that would otherwise paper over
    # as a successful exit but with an empty/error reply. The
    # Omnigent path has historically swallowed a few of these
    # (see designs/OMNIGENT_INTEGRATION.md gap log), so assert
    # up front.
    forbidden = (
        # 401 from OpenAI means the key in /tmp/mykey was
        # invalid or got routed to the wrong base URL.
        "401",
        # validator failure — the translator emitted a spec the
        # validator rejects.
        "invalid agent spec synthesized",
        # The pre-stdio-MCP translator fail-loud. If this shows
        # up, the MCPTool rejection regressed.
        "cannot yet translate",
    )
    for marker in forbidden:
        assert marker not in combined, (
            f"Forbidden marker {marker!r} in output — a --omnigent "
            f"failure mode fired. stderr tail:\n{result.stderr[-2000:]}"
        )

    assert result.returncode == 0, (
        f"--omnigent exited {result.returncode}. "
        f"stderr tail:\n{result.stderr[-2000:]}\n"
        f"stdout tail:\n{result.stdout[-2000:]}"
    )

    # The reply must contain ``echo: <probe>``. This is the only
    # assertion that catches the failure mode the whole stdio-
    # MCP work was motivated by: if the translator drops the MCP
    # tool silently, the LLM has no way to produce this string
    # in its reply (it can only echo text it was given, and the
    # prompt asks for the TOOL's return value).
    assert _SUCCESS_MARKER in result.stdout, (
        f"Expected {_SUCCESS_MARKER!r} in stdout but didn't "
        f"find it — the LLM either didn't call the ``echo`` "
        f"tool, or the tool's output never reached the reply. "
        f"stdout tail:\n{result.stdout[-2500:]}"
    )
