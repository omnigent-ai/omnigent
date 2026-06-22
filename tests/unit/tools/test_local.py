"""Tests for omnigent.tools.local (LocalPythonTool subprocess execution)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from omnigent.runner.identity import RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR
from omnigent.spec.types import LocalToolInfo, SandboxConfig
from omnigent.tools.base import ToolContext
from omnigent.tools.local import (
    LocalPythonTool,
    LocalToolLoadError,
    load_local_python_tools,
)

# ─── Helpers ────────────────────────────────────────────────────────


def _write_decorated_tool(
    tools_dir: Path,
    filename: str,
    *,
    func_name: str = "echo_tool",
    body: str = "return f'result: {value}'",
    extra_decoration: str = "",
    additional_funcs: str = "",
) -> None:
    """
    Write a Python file that defines a single ``@tool`` function.

    :param tools_dir: The ``tools/python/`` directory to write into.
    :param filename: File name, e.g. ``"echo_tool.py"``.
    :param func_name: The decorated function's name, e.g. ``"echo_tool"``.
    :param body: The body of the function (one or more statements
        separated by ``\\n    `` for the 4-space function indent).
        Must include a ``return``.
    :param extra_decoration: e.g. ``"(strict=False)"`` to apply
        ``@tool(strict=False)``. Default is bare ``@tool``.
    :param additional_funcs: Optional extra Python source appended
        below the primary function (used to test multi-tool files
        and load-error scenarios).
    """
    tools_dir.mkdir(parents=True, exist_ok=True)
    # Write the source verbatim with explicit indentation — no
    # textwrap.dedent gymnastics. The body parameter is inserted
    # with a 4-space prefix to land inside the function.
    body_lines = body.split("\n")
    indented_body = "\n".join(f"    {line}" for line in body_lines)
    code = (
        '"""Test tool."""\n'
        "from omnigent_client import tool\n"
        "\n"
        "\n"
        f"@tool{extra_decoration}\n"
        f"def {func_name}(value: str) -> str:\n"
        '    """A test tool."""\n'
        f"{indented_body}\n"
        "\n"
        f"{additional_funcs}\n"
    )
    (tools_dir / filename).write_text(code)


def _write_undecorated_module(tools_dir: Path, filename: str, schema_name: str) -> None:
    """
    Write a Python file that defines a function WITHOUT ``@tool``.

    Used by the regression test that verifies the loader fails
    loud when a tool file exports no decorated functions.

    :param tools_dir: The ``tools/python/`` directory.
    :param filename: File name, e.g. ``"no_decorator.py"``.
    :param schema_name: The function name to define (only matters
        for diagnostic clarity in the test).
    """
    tools_dir.mkdir(parents=True, exist_ok=True)
    code = textwrap.dedent(
        f'''\
        """Test tool file with no @tool decoration."""
        from typing import Any


        def {schema_name}(arguments: dict[str, Any]) -> str:
            """A function not decorated as a tool."""
            return "ok"
        '''
    )
    (tools_dir / filename).write_text(code)


@pytest.fixture
def tool_ctx() -> ToolContext:
    """A ToolContext for invoke() that doesn't need real workspace state."""
    return ToolContext(task_id="task_test", agent_id="ag_test", workspace=None)


# ─── Subprocess invocation ──────────────────────────────────────────


def test_invoke_subprocess_success(tmp_path: Path, tool_ctx: ToolContext) -> None:
    """A valid tool executes via subprocess and returns its result over fd 3."""
    py_dir = tmp_path / "tools" / "python"
    _write_decorated_tool(py_dir, "echo_tool.py", func_name="echo_tool")
    info = LocalToolInfo(name="echo_tool", path="tools/python/echo_tool.py", language="python")
    tools = load_local_python_tools([info], tmp_path)
    assert len(tools) == 1
    result = tools[0].invoke(json.dumps({"value": "hello"}), tool_ctx)
    # The tool body returns f'result: {value}', proving the args
    # actually traversed the subprocess pipeline.
    assert "hello" in result
    assert "result:" in result


def test_invoke_subprocess_strips_runner_binding_token(
    tmp_path: Path,
    tool_ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local tool's subprocess never sees the runner binding token.

    Local tools run spec-author-provided code in a child
    that inherited the runner's full ``os.environ``, so the agent
    payload could read the runner's control-plane auth secret. The
    tool body reports the token's presence in its own environment; the
    result must be ``ABSENT``. The benign marker proves the env was
    still forwarded (the strip didn't wipe the whole environment).

    :param tmp_path: Pytest temp dir for the generated tool file.
    :param tool_ctx: ToolContext for ``invoke()``.
    :param monkeypatch: Used to seed the binding token and a benign
        marker into the runner process's ``os.environ``.
    """
    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, "bug-binding-token-secret")
    monkeypatch.setenv("LOCAL_TOOL_ENV_MARKER", "marker-value")
    py_dir = tmp_path / "tools" / "python"
    body = (
        "import os\n"
        f"token = os.environ.get({RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR!r})\n"
        "marker = os.environ.get('LOCAL_TOOL_ENV_MARKER')\n"
        'return f\'token={"PRESENT" if token else "ABSENT"} marker={marker}\''
    )
    _write_decorated_tool(py_dir, "probe_tool.py", func_name="probe_tool", body=body)
    info = LocalToolInfo(name="probe_tool", path="tools/python/probe_tool.py", language="python")
    tools = load_local_python_tools([info], tmp_path)

    result = tools[0].invoke(json.dumps({"value": "x"}), tool_ctx)

    assert "token=ABSENT" in result, result
    assert "marker=marker-value" in result, result  # env still forwarded, just minus the secret


def test_invoke_subprocess_crash_isolation(tmp_path: Path, tool_ctx: ToolContext) -> None:
    """The tool runs in a subprocess (different pid from this process)."""
    py_dir = tmp_path / "tools" / "python"
    _write_decorated_tool(
        py_dir,
        "pid_tool.py",
        func_name="pid_tool",
        body="import os\nreturn str(os.getpid())",
    )
    info = LocalToolInfo(name="pid_tool", path="tools/python/pid_tool.py", language="python")
    tools = load_local_python_tools([info], tmp_path)
    pid_str = tools[0].invoke(json.dumps({"value": "ignored"}), tool_ctx)
    # Pid should be a valid number — and not the current process's pid.
    child_pid = int(pid_str.strip())
    assert child_pid != os.getpid(), (
        f"Tool ran in-process (pid {child_pid} == server pid {os.getpid()}). "
        "Subprocess isolation guarantee broken."
    )


def test_invoke_subprocess_exception(tmp_path: Path, tool_ctx: ToolContext) -> None:
    """A tool that raises an exception surfaces an error string."""
    py_dir = tmp_path / "tools" / "python"
    _write_decorated_tool(
        py_dir,
        "boom.py",
        func_name="boom",
        body="raise RuntimeError('intentional failure')",
    )
    info = LocalToolInfo(name="boom", path="tools/python/boom.py", language="python")
    tools = load_local_python_tools([info], tmp_path)
    result = tools[0].invoke(json.dumps({"value": "x"}), tool_ctx)
    # Error string should name the exception type and message so the
    # LLM can react meaningfully.
    assert "RuntimeError" in result
    assert "intentional failure" in result


def test_invoke_empty_args(tmp_path: Path, tool_ctx: ToolContext) -> None:
    """An empty arguments string is parsed as an empty dict."""
    py_dir = tmp_path / "tools" / "python"
    _write_decorated_tool(
        py_dir,
        "no_args.py",
        func_name="no_args",
        body="return 'noargs:' + value",
    )
    info = LocalToolInfo(name="no_args", path="tools/python/no_args.py", language="python")
    tools = load_local_python_tools([info], tmp_path)
    # Calling without an arg is a TypeError; framework surfaces it.
    result = tools[0].invoke("", tool_ctx)
    # The tool requires `value`; invocation with no args fails.
    assert "Error" in result or "missing" in result.lower()


def test_cancel_kills_subprocess(tmp_path: Path, tool_ctx: ToolContext) -> None:
    """``cancel()`` sends SIGKILL to the running subprocess."""
    py_dir = tmp_path / "tools" / "python"
    # Sleep for 60s so the test has a window to cancel.
    _write_decorated_tool(
        py_dir,
        "slow.py",
        func_name="slow",
        body="import time\ntime.sleep(60)\nreturn 'never'",
    )
    info = LocalToolInfo(name="slow", path="tools/python/slow.py", language="python")
    tools = load_local_python_tools([info], tmp_path)
    tool = tools[0]

    # Start the subprocess in a background thread; cancel from main.
    import threading

    result_holder: dict[str, str] = {}

    def _invoke() -> None:
        result_holder["result"] = tool.invoke(json.dumps({"value": "x"}), tool_ctx)

    thread = threading.Thread(target=_invoke, daemon=True)
    thread.start()
    # Wait for the subprocess to actually start so cancel has something to kill.
    import time

    deadline = time.time() + 5.0
    while time.time() < deadline:
        with tool._procs_lock:
            started = bool(tool._live_procs)
        if started:
            break
        time.sleep(0.05)
    with tool._procs_lock:
        assert tool._live_procs, "subprocess never started"

    tool.cancel()
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "invoke() did not return after cancel()"
    # Result should be an error (subprocess killed → no response).
    assert "Error" in result_holder["result"]


# ─── Loader ─────────────────────────────────────────────────────────


def test_load_single_decorated_tool(tmp_path: Path) -> None:
    """A file with one ``@tool`` produces one LocalPythonTool."""
    py_dir = tmp_path / "tools" / "python"
    _write_decorated_tool(py_dir, "single.py", func_name="single")
    info = LocalToolInfo(name="single", path="tools/python/single.py", language="python")
    tools = load_local_python_tools([info], tmp_path)
    assert len(tools) == 1
    assert tools[0].name() == "single"


def test_load_multiple_tools_in_one_file(tmp_path: Path) -> None:
    """G16: Multiple ``@tool``-decorated functions in one file → multiple tools."""
    py_dir = tmp_path / "tools" / "python"
    py_dir.mkdir(parents=True)
    multi = textwrap.dedent(
        '''\
        """Multi-tool file."""
        from omnigent_client import tool


        @tool
        def first(x: str) -> str:
            """First."""
            return x


        @tool
        def second(y: int) -> int:
            """Second."""
            return y * 2


        def not_a_tool() -> None:
            """Helper that should NOT be exposed as a tool."""
        '''
    )
    (py_dir / "multi.py").write_text(multi)
    info = LocalToolInfo(name="multi", path="tools/python/multi.py", language="python")
    tools = load_local_python_tools([info], tmp_path)
    # Exactly two tools — `not_a_tool` is undecorated and must be ignored.
    assert len(tools) == 2
    names = sorted(tool.name() for tool in tools)
    assert names == ["first", "second"]


def test_load_multiple_files(tmp_path: Path) -> None:
    """Multiple files each with one tool → list of N tools."""
    py_dir = tmp_path / "tools" / "python"
    _write_decorated_tool(py_dir, "a.py", func_name="alpha")
    _write_decorated_tool(py_dir, "b.py", func_name="beta")
    infos = [
        LocalToolInfo(name="a", path="tools/python/a.py", language="python"),
        LocalToolInfo(name="b", path="tools/python/b.py", language="python"),
    ]
    tools = load_local_python_tools(infos, tmp_path)
    assert sorted(tool.name() for tool in tools) == ["alpha", "beta"]


def test_load_skips_typescript(tmp_path: Path) -> None:
    """Non-Python tools are silently skipped."""
    info = LocalToolInfo(name="ts_tool", path="tools/typescript/ts_tool.ts", language="typescript")
    tools = load_local_python_tools([info], tmp_path)
    assert tools == []


def test_load_missing_file_fails_loud(tmp_path: Path) -> None:
    """A declared-but-nonexistent file raises ``LocalToolLoadError``."""
    info = LocalToolInfo(name="ghost", path="tools/python/ghost.py", language="python")
    with pytest.raises(LocalToolLoadError, match="not found"):
        load_local_python_tools([info], tmp_path, agent_name="testagent")


def test_load_file_with_no_tool_decorator_fails_loud(tmp_path: Path) -> None:
    """A file that defines functions without ``@tool`` fails to load."""
    py_dir = tmp_path / "tools" / "python"
    _write_undecorated_module(py_dir, "legacy.py", schema_name="legacy")
    info = LocalToolInfo(name="legacy", path="tools/python/legacy.py", language="python")
    with pytest.raises(LocalToolLoadError, match="@tool") as exc_info:
        load_local_python_tools([info], tmp_path, agent_name="testagent")
    # The error must name the agent and the file path so authors can
    # navigate directly to the offending file.
    msg = str(exc_info.value)
    assert "testagent" in msg
    assert "legacy.py" in msg


def test_load_no_decorated_functions_fails_loud(tmp_path: Path) -> None:
    """A file with zero ``@tool`` functions raises ``LocalToolLoadError``."""
    py_dir = tmp_path / "tools" / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "empty.py").write_text("# no @tool functions here\n")
    info = LocalToolInfo(name="empty", path="tools/python/empty.py", language="python")
    with pytest.raises(LocalToolLoadError, match="no @tool"):
        load_local_python_tools([info], tmp_path, agent_name="testagent")


def test_load_import_error_actionable(tmp_path: Path) -> None:
    """An ImportError inside a tool file surfaces with file + cause."""
    py_dir = tmp_path / "tools" / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "broken.py").write_text("import this_module_definitely_does_not_exist\n")
    info = LocalToolInfo(name="broken", path="tools/python/broken.py", language="python")
    with pytest.raises(LocalToolLoadError, match="failed to import") as exc_info:
        load_local_python_tools([info], tmp_path, agent_name="testagent")
    msg = str(exc_info.value)
    assert "testagent" in msg
    assert "broken.py" in msg


def test_load_collision_across_files_fails_loud(tmp_path: Path) -> None:
    """G27: two custom tools sharing a name across files fails loud."""
    py_dir = tmp_path / "tools" / "python"
    _write_decorated_tool(py_dir, "first.py", func_name="duplicate")
    _write_decorated_tool(py_dir, "second.py", func_name="duplicate")
    infos = [
        LocalToolInfo(name="first", path="tools/python/first.py", language="python"),
        LocalToolInfo(name="second", path="tools/python/second.py", language="python"),
    ]
    with pytest.raises(LocalToolLoadError, match="collision") as exc_info:
        load_local_python_tools(infos, tmp_path, agent_name="testagent")
    msg = str(exc_info.value)
    # Both source paths must appear so the author knows which two
    # files are in conflict.
    assert "first.py" in msg
    assert "second.py" in msg
    assert "duplicate" in msg


def test_load_collision_with_builtin_fails_loud(tmp_path: Path) -> None:
    """G27: custom tool whose name matches a builtin fails at load."""
    py_dir = tmp_path / "tools" / "python"
    _write_decorated_tool(py_dir, "ws.py", func_name="web_search")
    info = LocalToolInfo(name="ws", path="tools/python/ws.py", language="python")
    with pytest.raises(LocalToolLoadError, match="collision") as exc_info:
        load_local_python_tools(
            [info],
            tmp_path,
            agent_name="testagent",
            builtin_tool_names=frozenset({"web_search"}),
        )
    msg = str(exc_info.value)
    # The error must name both the custom file and the builtin so
    # the author can choose which to keep.
    assert "ws.py" in msg
    assert "web_search" in msg
    assert "builtin" in msg.lower() or "built-in" in msg.lower()


# ─── Command building ───────────────────────────────────────────────


def _make_tool(
    tmp_path: Path,
    *,
    has_inline_deps: bool = False,
    inline_deps: list[str] | None = None,
    container_image: str | None = None,
    docker_image: str | None = None,
    container_runtime: str = "docker",
    srt_available: bool = False,
    uv_available: bool = False,
    sandbox_enabled: bool = True,
) -> LocalPythonTool:
    """Build a :class:`LocalPythonTool` for command-construction tests."""
    py_dir = tmp_path / "tools" / "python"
    _write_decorated_tool(py_dir, "demo.py", func_name="demo")
    info = LocalToolInfo(
        name="demo",
        path="tools/python/demo.py",
        language="python",
        has_inline_deps=has_inline_deps,
        inline_deps=inline_deps,
    )
    sandbox_config = SandboxConfig(
        container_image=container_image,
        docker_image=docker_image,
        container_runtime=container_runtime,
    )
    tools = load_local_python_tools(
        [info],
        tmp_path,
        sandbox_config=sandbox_config,
        srt_available=srt_available,
        uv_available=uv_available,
        sandbox_enabled=sandbox_enabled,
    )
    return tools[0]


def test_build_command_plain(tmp_path: Path) -> None:
    """No srt, no uv → ``[python, _runner.py]``."""
    tool = _make_tool(tmp_path)
    cmd = tool._build_command(state_root=None)
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("_runner.py")


def test_build_command_with_uv(tmp_path: Path) -> None:
    """PEP 723 deps + uv → ``uv run --with <dep> -- python _runner.py``."""
    tool = _make_tool(
        tmp_path,
        has_inline_deps=True,
        inline_deps=["ftfy>=6.0"],
        uv_available=True,
    )
    cmd = tool._build_command(state_root=None)
    assert cmd[:2] == ["uv", "run"]
    assert "--with" in cmd
    assert "ftfy>=6.0" in cmd
    assert "--" in cmd
    assert "python" in cmd


def test_build_command_with_srt(tmp_path: Path) -> None:
    """srt available + sandbox → ``srt -c '<command>'``."""
    tool = _make_tool(tmp_path, srt_available=True, sandbox_enabled=True)
    cmd = tool._build_command(state_root=None)
    assert cmd[0] == "srt"
    assert cmd[1] == "-c"


def test_build_command_srt_disabled(tmp_path: Path) -> None:
    """srt available but sandbox disabled → no srt prefix."""
    tool = _make_tool(tmp_path, srt_available=True, sandbox_enabled=False)
    cmd = tool._build_command(state_root=None)
    assert cmd[0] == sys.executable


def test_build_command_container(tmp_path: Path) -> None:
    """container_image set → docker run command (default runtime)."""
    tool = _make_tool(tmp_path, container_image="python:3.11")
    cmd = tool._build_command(state_root=None)
    assert cmd[0] == "docker"
    assert "run" in cmd
    assert "python:3.11" in cmd


def test_build_command_docker_image_alias(tmp_path: Path) -> None:
    """docker_image (deprecated alias) still works."""
    tool = _make_tool(tmp_path, docker_image="python:3.11")
    cmd = tool._build_command(state_root=None)
    assert cmd[0] == "docker"
    assert "python:3.11" in cmd


def test_build_command_podman(tmp_path: Path) -> None:
    """container_runtime='podman' → podman run command with network isolation."""
    tool = _make_tool(tmp_path, container_image="python:3.11", container_runtime="podman")
    cmd = tool._build_command(state_root=None)
    assert cmd[0] == "podman"
    assert "run" in cmd
    assert "python:3.11" in cmd
    assert "--network" in cmd
    net_idx = cmd.index("--network")
    assert cmd[net_idx + 1] == "none"


def test_build_command_container_network_isolation(tmp_path: Path) -> None:
    """Both runtimes include --network none for sandbox isolation."""
    for runtime in ("docker", "podman"):
        tool = _make_tool(tmp_path, container_image="python:3.11", container_runtime=runtime)
        cmd = tool._build_command(state_root=None)
        assert "--network" in cmd, f"{runtime}: missing --network flag"
        net_idx = cmd.index("--network")
        assert cmd[net_idx + 1] == "none", f"{runtime}: --network not set to none"


def test_sandbox_config_rejects_invalid_runtime() -> None:
    """SandboxConfig.__post_init__ rejects unknown container_runtime values."""
    with pytest.raises(ValueError, match="container_runtime"):
        SandboxConfig(container_runtime="rkt")  # type: ignore[arg-type]


# ─── Schema + name plumbing ─────────────────────────────────────────


def test_tool_get_schema_uses_metadata_name_and_description(
    tmp_path: Path,
) -> None:
    """The wire-format schema uses the function name and docstring."""
    py_dir = tmp_path / "tools" / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "doctool.py").write_text(
        textwrap.dedent(
            '''\
            """Doctool file."""
            from omnigent_client import tool


            @tool
            def with_docs(text: str, count: int = 1) -> str:
                """Repeat the text count times."""
                return text * count
            '''
        )
    )
    info = LocalToolInfo(name="doctool", path="tools/python/doctool.py", language="python")
    tools = load_local_python_tools([info], tmp_path)
    schema = tools[0].get_schema()
    # Wire-format: {"type":"function","function":{...}}
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "with_docs"
    assert schema["function"]["description"] == "Repeat the text count times."
    # Parameters is the strict-normalized JSON schema.
    params = schema["function"]["parameters"]
    assert params["type"] == "object"
    assert "text" in params["properties"]
    assert "count" in params["properties"]


# ─── PEP 723 ────────────────────────────────────────────────────────


def test_pep723_scanning_at_load_time(tmp_path: Path) -> None:
    """A file with PEP 723 inline deps is detected at load time."""
    py_dir = tmp_path / "tools" / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "with_deps.py").write_text(
        textwrap.dedent(
            '''\
            # /// script
            # dependencies = ["requests>=2.0"]
            # ///
            """A tool with PEP 723 deps."""
            from omnigent_client import tool


            @tool
            def with_deps(value: str) -> str:
                """Doc."""
                return value
            '''
        )
    )
    info = LocalToolInfo(name="with_deps", path="tools/python/with_deps.py", language="python")
    tools = load_local_python_tools([info], tmp_path)
    # Loader mutates info in place.
    assert info.has_inline_deps is True
    assert info.inline_deps == ["requests>=2.0"]
    assert len(tools) == 1


# ─── Runner integration (subprocess execution end-to-end) ───────────


def _run_runner_with_request(tool_path: Path, tool_name: str, arguments: dict) -> dict:
    """
    Spawn the runner subprocess and return its parsed JSON response.

    Uses fd 3 protocol so the test mirrors the real production
    invocation path, not the Docker fallback.
    """
    runner = Path(__file__).parent.parent.parent.parent / "omnigent" / "tools" / "_runner.py"
    request = json.dumps(
        {
            "module_path": str(tool_path),
            "tool_name": tool_name,
            "arguments": arguments,
        }
    ).encode()

    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.Popen(
            [sys.executable, str(runner)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(write_fd,),
            env={**os.environ, "_AP_RESPONSE_FD": str(write_fd)},
        )
        os.close(write_fd)
        write_fd = -1
        proc.communicate(input=request, timeout=10)
        raw = os.read(read_fd, 1024 * 1024)
        return json.loads(raw)
    finally:
        if write_fd != -1:
            os.close(write_fd)
        os.close(read_fd)


def test_runner_dispatches_to_named_function(tmp_path: Path) -> None:
    """The runner dispatches to the function named in the request."""
    py_dir = tmp_path / "tools" / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "multi.py").write_text(
        textwrap.dedent(
            '''\
            """Multi-tool file."""
            from omnigent_client import tool


            @tool
            def alpha(x: str) -> str:
                """A."""
                return f"alpha:{x}"


            @tool
            def beta(x: str) -> str:
                """B."""
                return f"beta:{x}"
            '''
        )
    )
    response = _run_runner_with_request(py_dir / "multi.py", "alpha", {"x": "hello"})
    # Response carries the alpha-formatted result, not beta's.
    assert "alpha:hello" in response.get("result", "")


def test_runner_rejects_undecorated_function(tmp_path: Path) -> None:
    """The runner refuses to invoke a function lacking the @tool marker."""
    py_dir = tmp_path / "tools" / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "mixed.py").write_text(
        textwrap.dedent(
            '''\
            """Mixed file with both decorated and bare functions."""
            from omnigent_client import tool


            @tool
            def decorated(x: str) -> str:
                """OK."""
                return x


            def bare(x: str) -> str:
                """Not a tool."""
                return x
            '''
        )
    )
    response = _run_runner_with_request(py_dir / "mixed.py", "bare", {"x": "ignored"})
    # Calling a non-decorated function should fail with a clear error.
    assert "error" in response
    assert "@tool" in response["error"]


def test_runner_import_error(tmp_path: Path) -> None:
    """The runner returns a clear error when the tool module can't import."""
    py_dir = tmp_path / "tools" / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "bad.py").write_text("import nonexistent_module_xyz\n")
    response = _run_runner_with_request(py_dir / "bad.py", "any_name", {})
    assert "error" in response
    assert "Import error" in response["error"]


def test_runner_runtime_error(tmp_path: Path) -> None:
    """The runner reports runtime exceptions back to the parent."""
    py_dir = tmp_path / "tools" / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "boom.py").write_text(
        textwrap.dedent(
            '''\
            """Tool that always raises."""
            from omnigent_client import tool


            @tool
            def boom(value: str) -> str:
                """Always crashes."""
                raise ValueError(f"boom: {value}")
            '''
        )
    )
    response = _run_runner_with_request(py_dir / "boom.py", "boom", {"value": "BOOM"})
    assert "error" in response
    # Error string must include the exception class and message so
    # the framework can surface it intelligibly to the LLM.
    assert "ValueError" in response["error"]
    assert "BOOM" in response["error"]


def test_runner_serializes_dict_return(tmp_path: Path) -> None:
    """A dict return value comes back as a JSON-encoded string."""
    py_dir = tmp_path / "tools" / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "dictret.py").write_text(
        textwrap.dedent(
            '''\
            """Returns a dict."""
            from omnigent_client import tool


            @tool
            def dictret(value: str) -> dict[str, str]:
                """Wrap."""
                return {"key": value}
            '''
        )
    )
    response = _run_runner_with_request(py_dir / "dictret.py", "dictret", {"value": "abc"})
    # Result is a JSON string the LLM can parse directly.
    parsed = json.loads(response["result"])
    assert parsed == {"key": "abc"}


def test_runner_passes_string_return_unchanged(tmp_path: Path) -> None:
    """A str return is passed through unchanged (no extra JSON-quoting)."""
    py_dir = tmp_path / "tools" / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "strret.py").write_text(
        textwrap.dedent(
            '''\
            """Returns a string."""
            from omnigent_client import tool


            @tool
            def strret(value: str) -> str:
                """Echo."""
                return f"hello {value}"
            '''
        )
    )
    response = _run_runner_with_request(py_dir / "strret.py", "strret", {"value": "world"})
    # No extra quoting / JSON wrapping for str returns.
    assert response["result"] == "hello world"


def test_concurrent_invoke_does_not_race_on_instance_state(
    tmp_path: Path,
    tool_ctx: ToolContext,
) -> None:
    """Concurrent invocations on the same tool instance must not race.

    Regression test: ``LocalPythonTool`` previously stashed the
    live subprocess on ``self._proc`` during each ``invoke()``
    call and reset it to ``None`` in the ``finally``. With
    multiple concurrent tool calls on the same instance (the
    runtime dispatches parallel ``function_call`` items), one
    call's ``self._proc = None`` would race another call's
    ``self._proc.returncode`` read and raise
    ``AttributeError: 'NoneType' object has no attribute
    'returncode'``.

    What this test verifies:
    * All N concurrent invocations return a non-error result
      string (so no ``AttributeError`` bubbled through).
    * No call returns the "Error:" sentinel prefix that
      ``_invoke_subprocess`` emits on subprocess failure — the
      race produced exactly that failure mode in practice.
    * After all calls complete, ``_live_procs`` is empty (cleanup
      ran on every path).

    Reasonable N (16) is enough to catch the race: if the old
    single-``self._proc`` code reappears, even 2 concurrent calls
    would flake. 16 makes the regression unmissable on CI.

    The testing skill's "concurrency test requirements" (blocked
    LLM call + release) don't apply here — this is not a workflow
    test. The blocked-call pattern exists to freeze an LLM
    response at a known point; we're instead testing the
    Python-level race inside ``LocalPythonTool.invoke``, and the
    ``threading.Barrier`` is the analogous synchronization
    primitive: it forces N workers to enter ``invoke()`` at the
    same wall-clock moment so the instance-state race actually
    races.
    """
    py_dir = tmp_path / "tools" / "python"
    _write_decorated_tool(py_dir, "echo_concurrent.py", func_name="echo_concurrent")
    info = LocalToolInfo(
        name="echo_concurrent",
        path="tools/python/echo_concurrent.py",
        language="python",
    )
    # ``srt_available=False`` skips srt sandbox wrapping. srt has its
    # own concurrency bug ("Shell 'bash' not found in PATH",
    # "ripgrep (rg) not found") that surfaces under heavy parallel
    # invocation and would corrupt this test's race-detection — we
    # want the assertions to fail on the ``self._proc`` race we're
    # actually testing, not on srt's flake.
    tools = load_local_python_tools([info], tmp_path, srt_available=False)
    tool = tools[0]

    num_calls = 16
    results: list[str] = [""] * num_calls
    errors: list[BaseException | None] = [None] * num_calls
    # Start-gate: every worker blocks on the barrier so all N
    # invocations enter ``invoke()`` around the same wall-clock
    # moment. Without this, threads serialize naturally and the
    # race window shrinks.
    barrier = threading.Barrier(num_calls)

    def _worker(i: int) -> None:
        barrier.wait()
        try:
            results[i] = tool.invoke(json.dumps({"value": f"v{i}"}), tool_ctx)
        except Exception as exc:
            errors[i] = exc

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(num_calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)
        assert not t.is_alive(), "worker did not complete"

    # No thread raised — the NoneType bug would surface here.
    for i, err in enumerate(errors):
        assert err is None, f"worker {i} raised {err!r}"

    # Every call produced its own distinct result. If the race
    # had swapped outputs between calls, the mapping would break.
    for i, r in enumerate(results):
        assert not r.startswith("Error:"), (
            f"worker {i} got error string {r!r}. A NoneType race on "
            f"self._proc.returncode surfaces as Error: here."
        )
        assert f"v{i}" in r, (
            f"worker {i} got {r!r}, doesn't contain its own input 'v{i}'. "
            f"If two calls' arguments got crossed, the instance state "
            f"is still being shared improperly."
        )

    # Cleanup ran on every finally — no live procs leaked.
    with tool._procs_lock:
        leaked = list(tool._live_procs)
    assert leaked == [], (
        f"_live_procs should be empty after all calls complete, got {leaked}. "
        f"A finally branch is skipping the discard() call."
    )
