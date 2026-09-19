"""Local Python tool execution via subprocess.

Loads ``@tool``-decorated functions from the agent's
``tools/python/`` directory and exposes each as a
:class:`LocalPythonTool` instance. A single Python file may
export multiple tools (one per ``@tool`` function); discovery runs
in the subprocess and the loader expands one
:class:`LocalToolInfo` (file-level) into N
``LocalPythonTool`` instances.

Tool code runs in a **subprocess** (not in-process) for crash
isolation. Communication uses the fd 3 pipe protocol — see
``_runner.py`` for the child side. The subprocess invocation
identifies the target ``@tool`` function by name, since one
file may host several.

Execution tiers (in priority order):

1. **Container** — ``docker run`` or ``podman run`` with network
   disabled. Used when ``sandbox.container_image`` is configured.
   The runtime is selected via ``sandbox.container_runtime``.
2. **srt + uv** — ``srt uv run --with ... -- python _runner.py``.
   Used when srt is on PATH, sandbox enabled, and tool has PEP 723
   inline deps.
3. **srt** — ``srt python _runner.py``. Used when srt is on PATH
   and sandbox enabled.
4. **uv** — ``uv run --with ... -- python _runner.py``. Used when
   tool has PEP 723 inline deps and uv is available.
5. **plain** — ``python _runner.py``. Default fallback.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path

# Any: OpenAI function schemas contain heterogeneous values
# (strings, ints, nested objects, arrays) — no specific type fits.
from typing import Any

from omnigent_client.tools import ToolMetadata

from omnigent.runner.identity import strip_runner_auth_secrets
from omnigent.spec.types import LocalToolInfo, SandboxConfig, ToolRuntime
from omnigent.tools._pep723 import parse_inline_metadata
from omnigent.tools._srt import wrap_with_srt
from omnigent.tools.base import Tool, ToolContext

_logger = logging.getLogger(__name__)

# Absolute path to the runner script. Resolved once at import time
# so subprocess invocations don't depend on cwd.
_RUNNER_PATH = str(Path(__file__).parent / "_runner.py")

# Maximum bytes to read from the fd 3 response pipe (1 MiB).
_MAX_RESPONSE_BYTES = 1024 * 1024

# Prefix used by the runner in Docker/stdout mode.
_STDOUT_RESPONSE_PREFIX = "__AP_RESPONSE__:"

_DISCOVERY_TIMEOUT_SECONDS = 60.0


class LocalToolLoadError(Exception):
    """
    Raised when an agent image's local tool files fail to load.

    Surfaces a single actionable error per agent image. Carries
    enough context (agent name, file path, function name, cause)
    that authors can fix the offending file without further
    debugging.
    """


class LocalPythonTool(Tool):
    """
    A tool backed by a ``@tool``-decorated function in a local Python file.

    One file may export multiple tools; the framework instantiates
    one :class:`LocalPythonTool` per decorated function. The
    subprocess runner re-imports the file and dispatches to the
    named function.

    :param info: The discovered :class:`LocalToolInfo` for the
        file this tool lives in.
    :param metadata: The :class:`ToolMetadata` extracted from the
        ``@tool``-decorated function in the discovery subprocess.
    :param module_path: Absolute path to the tool Python file.
    :param sandbox_config: Sandbox settings from the agent spec.
    :param srt_available: Whether ``srt`` is on PATH.
    :param uv_available: Whether ``uv`` is on PATH.
    :param sandbox_enabled: Runtime policy for srt sandboxing.
    """

    def __init__(
        self,
        info: LocalToolInfo,
        metadata: ToolMetadata,
        module_path: Path,
        sandbox_config: SandboxConfig,
        srt_available: bool,
        uv_available: bool,
        sandbox_enabled: bool = True,
        uses_tool_state: bool = False,
    ) -> None:
        """
        Initialize from a discovered ``@tool`` function.

        :param info: The :class:`LocalToolInfo` for the source file.
        :param metadata: The :class:`ToolMetadata` produced by
            ``@tool`` at decoration time.
        :param module_path: Absolute path to the tool file, e.g.
            ``Path("/tmp/cache/ag_abc/tools/python/my_tools.py")``.
        :param sandbox_config: Agent-level sandbox settings
            (container_image, container_runtime).
        :param srt_available: Whether ``srt`` is on PATH.
        :param uv_available: Whether ``uv`` is on PATH.
        :param sandbox_enabled: Runtime policy for srt sandboxing.
        """
        self._info = info
        self._metadata = metadata
        self._module_path = module_path
        self._sandbox_config = sandbox_config
        self._sandbox_enabled = sandbox_enabled
        self._srt_available = srt_available
        self._uv_available = uv_available
        self._uses_tool_state = uses_tool_state
        # Live subprocesses from any in-flight ``invoke()`` — tracked
        # as a set guarded by a lock so ``cancel()`` can kill all of
        # them at once. A single ``self._proc`` would race when the
        # runtime dispatches multiple concurrent tool calls on the
        # same tool instance (six parallel add_task calls on a
        # stateful tool, for example): each overwrites the previous
        # and ``self._proc.returncode`` reads fail with None.
        self._live_procs: set[subprocess.Popen[bytes]] = set()
        self._procs_lock = threading.Lock()

    def name(self) -> str:  # type: ignore[override]
        """
        Tool name derived from the ``@tool``-decorated function's ``__name__``.

        :returns: The tool name as the LLM sees it, e.g. ``"word_count"``.
        """
        return self._metadata.name

    # ``is_async`` and ``dispatch_async`` deliberately not
    # overridden. Every ``@tool``-decorated function is sync
    # from the framework's perspective post-step-11; async
    # dispatch is the LLM's per-call choice via
    # ``sys_call_async`` (which calls
    # ``omnigent.runtime.workflow._dispatch_local_python_tool_async``
    # directly, not through ``LocalPythonTool.dispatch_async``).
    # The ``Tool`` base class default ``is_async`` returns
    # ``False`` and ``dispatch_async`` raises
    # ``NotImplementedError``, which is exactly the right
    # contract for this class.

    def module_path(self) -> str:
        """
        Return the absolute path to the tool's source file.

        Used by the background-tool-workflow dispatch path so the
        runner subprocess knows which file to import. Exposed here
        rather than reading ``_module_path`` directly from outside
        the class.

        :returns: Absolute path string, e.g.
            ``"/tmp/cache/ag_abc/tools/python/my_tools.py"``.
        """
        return str(self._module_path)

    @classmethod
    def description(cls) -> str:
        """
        :returns: Generic class-level description; per-instance
            descriptions come from each function's docstring via
            :meth:`get_schema`.
        """
        return "Custom local Python tool."

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI function-format tool schema.

        Composes the metadata's name + description + JSON schema
        into the wire-format the framework's tool-dispatch layer
        expects.

        :returns: A dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": self._metadata.name,
                "description": self._metadata.description,
                "parameters": self._metadata.json_schema,
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Execute the tool in a subprocess via the fd 3 protocol.

        Builds the command via :meth:`_build_command`, spawns the
        subprocess, sends the request on stdin, and reads the JSON
        response from fd 3 (or stdout in Docker mode). The request
        carries the target function name so the runner knows which
        ``@tool`` to dispatch (one file may export several).

        :param arguments: JSON-encoded arguments string from the LLM.
        :param ctx: Server-side execution context (unused by
            local tools, required by the :class:`Tool` interface).
        :returns: The tool's string result, or an error string
            if the subprocess fails.
        """
        parsed: dict[str, Any] = json.loads(arguments) if arguments else {}

        # Per-agent ToolState directory. Only available when the
        # workspace is present (tests without a workspace get None;
        # the runner then refuses to inject and raises a clear error
        # if the tool asked for tool_state). See designs/TOOL_STATE.md.
        state_root: str | None = None
        if self._uses_tool_state and ctx.workspace is not None:
            state_dir = ctx.workspace / ".tool_state" / ctx.agent_id
            state_dir.mkdir(parents=True, exist_ok=True)
            state_root = str(state_dir)

        request = json.dumps(
            {
                "module_path": str(self._module_path),
                "tool_name": self._metadata.name,
                "arguments": parsed,
                "state_root": state_root,
            }
        ).encode()

        # srt and Docker both wrap the command in their own process
        # chain, so the fd 3 pipe doesn't survive to the inner
        # Python process. Use the stdout protocol instead.
        use_stdout = _uses_stdout_protocol(
            self._sandbox_config,
            self._srt_available,
            self._sandbox_enabled,
        )
        cmd = self._build_command(state_root=state_root)
        if use_stdout:
            return self._invoke_stdout(cmd, request, workspace=ctx.workspace)
        return self._invoke_subprocess(cmd, request, workspace=ctx.workspace)

    def _invoke_subprocess(
        self,
        cmd: list[str],
        request: bytes,
        *,
        workspace: Path | None,
    ) -> str:
        """
        Run the tool via a local subprocess with fd 3 pipe.

        :param cmd: Command list to spawn.
        :param request: JSON-encoded request bytes.
        :param workspace: Per-conversation workspace path, forwarded
            via ``_AP_WORKSPACE``. ``None`` skips the env var.
        :returns: Tool result or error string.
        """
        read_fd, write_fd = os.pipe()
        try:
            # Strip the runner-auth secret: a local tool runs spec-author
            # code, which must never see the binding token.
            env = {**strip_runner_auth_secrets(os.environ), "_AP_RESPONSE_FD": str(write_fd)}
            if workspace is not None:
                env["_AP_WORKSPACE"] = str(workspace)
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(write_fd,),
                env=env,
            )
            with self._procs_lock:
                self._live_procs.add(proc)
            os.close(write_fd)
            write_fd = -1
            try:
                _stdout, stderr = proc.communicate(input=request)
                return _read_fd3_response(read_fd, proc.returncode, stderr)
            finally:
                with self._procs_lock:
                    self._live_procs.discard(proc)
        finally:
            if write_fd != -1:
                os.close(write_fd)
            os.close(read_fd)

    def _invoke_stdout(
        self,
        cmd: list[str],
        request: bytes,
        *,
        workspace: Path | None,
    ) -> str:
        """
        Run the tool via stdout protocol (for srt and Docker).

        When the command is wrapped by srt or Docker, the fd 3 pipe
        doesn't survive to the inner Python process. The runner
        writes the response to stdout with a ``__AP_RESPONSE__:``
        prefix instead.

        :param cmd: Command list to spawn.
        :param request: JSON-encoded request bytes.
        :param workspace: Per-conversation workspace path, forwarded
            via ``_AP_WORKSPACE``. ``None`` skips the env var.
        :returns: Tool result or error string.
        """
        # Strip the runner-auth secret before handing the env to
        # spec-author tool code; see _invoke_subprocess.
        env = {**strip_runner_auth_secrets(os.environ), "_AP_RESPONSE_MODE": "stdout"}
        if workspace is not None:
            env["_AP_WORKSPACE"] = str(workspace)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        with self._procs_lock:
            self._live_procs.add(proc)
        try:
            stdout, stderr = proc.communicate(input=request)
            return _read_stdout_response(stdout, proc.returncode, stderr)
        finally:
            with self._procs_lock:
                self._live_procs.discard(proc)

    def _build_command(self, *, state_root: str | None) -> list[str]:
        """
        Build the subprocess command based on execution tier.

        Priority: Docker > srt+uv > srt > uv > plain.

        :param state_root: Per-call ToolState directory (or ``None``
            for stateless tools). Threaded through so the srt
            settings file, written per invocation, whitelists the
            right path — passing via ``self`` would race between
            concurrent ``invoke()`` calls.
        :returns: The command list for ``subprocess.Popen``.
        """
        return _build_runner_command(
            info=self._info,
            sandbox_config=self._sandbox_config,
            srt_available=self._srt_available,
            uv_available=self._uv_available,
            sandbox_enabled=self._sandbox_enabled,
            state_root=state_root,
        )

    def cancel(self) -> None:
        """
        Kill every in-flight subprocess on timeout.

        Called by ``call_tool_with_timeout`` when the deadline
        expires. Sends SIGKILL to each tracked subprocess; any
        parallel invocations in progress at the same time all get
        cancelled, which matches the semantics of "this tool's
        deadline expired."
        """
        with self._procs_lock:
            procs = list(self._live_procs)
        for proc in procs:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    def shutdown(self) -> None:
        """Kill any remaining in-flight subprocesses on teardown."""
        self.cancel()


def _uses_stdout_protocol(
    sandbox_config: SandboxConfig,
    srt_available: bool,
    sandbox_enabled: bool,
) -> bool:
    """Return whether the command wrapper requires stdout responses."""
    return sandbox_config.container_image is not None or (srt_available and sandbox_enabled)


def _build_runner_command(
    *,
    info: LocalToolInfo,
    sandbox_config: SandboxConfig,
    srt_available: bool,
    uv_available: bool,
    sandbox_enabled: bool,
    state_root: str | None,
) -> list[str]:
    """
    Build the subprocess command based on execution tier.

    Priority: Docker > srt+uv > srt > uv > plain.

    :param info: Local tool file metadata.
    :param sandbox_config: Agent-level sandbox settings.
    :param srt_available: Whether ``srt`` is on PATH.
    :param uv_available: Whether ``uv`` is on PATH.
    :param sandbox_enabled: Runtime policy for srt sandboxing.
    :param state_root: Per-call ToolState directory (or ``None``
        for stateless tools).
    :returns: The command list for ``subprocess.Popen``.
    """
    if sandbox_config.container_image is not None:
        return _build_container_command(sandbox_config)
    base = [sys.executable, _RUNNER_PATH]
    if info.has_inline_deps and uv_available:
        return _build_uv_command(info, srt_available, sandbox_enabled)
    return _prepend_srt(base, srt_available, sandbox_enabled, state_root)


def _build_uv_command(
    info: LocalToolInfo,
    srt_available: bool,
    sandbox_enabled: bool,
) -> list[str]:
    """
    Build a ``uv run --with`` command for tools with PEP 723 deps.

    When srt is also active, uv runs OUTSIDE srt (it needs
    network for pypi and write access to its cache). srt wraps
    only the inner ``python _runner.py`` via uv's ``--``
    separator. Without srt, uv wraps the plain python command.

    Uses ``python`` (not ``sys.executable``) so uv's ephemeral
    venv Python is used and can see installed deps.

    :param info: Local tool file metadata.
    :param srt_available: Whether ``srt`` is on PATH.
    :param sandbox_enabled: Runtime policy for srt sandboxing.
    :returns: The uv command list.
    """
    uv_args: list[str] = ["uv", "run"]
    for dep in info.inline_deps or []:
        uv_args.extend(["--with", dep])
    if srt_available and sandbox_enabled:
        inner = shlex.join(["python", _RUNNER_PATH])
        uv_args.extend(["--", "srt", "-c", inner])
    else:
        uv_args.extend(["--", "python", _RUNNER_PATH])
    return uv_args


def _prepend_srt(
    cmd: list[str],
    srt_available: bool,
    sandbox_enabled: bool,
    state_root: str | None,
) -> list[str]:
    """
    Prepend ``srt`` if sandbox is enabled and available.

    Stateless tools use plain ``srt -c`` — srt's default
    bubblewrap sandbox is permissive enough for pure reads
    (the venv python remains executable, $PATH resolves,
    etc.).

    Stateful tools (those with a ``tool_state`` parameter)
    additionally need one writable path: ``{workspace}/.tool_state/
    {agent_id}/``. We achieve that with an ``-s`` settings
    file that keeps srt's permissive defaults for reads but
    whitelists exactly that directory for writes. This is
    NOT the same ``_srt_wrap.mjs``-based setup
    ``code_sandbox`` uses — that config restricts reads too,
    which would hide the venv python from the runner.

    The core enabled-and-available wrap is delegated to the
    shared :func:`~omnigent.tools._srt.wrap_with_srt` helper
    so the MCP stdio path shares the exact same on/off
    semantics; this method only builds the per-call
    ``settings_file`` for stateful tools and hands it in.

    :param cmd: The base command to wrap.
    :param srt_available: Whether ``srt`` is on PATH.
    :param sandbox_enabled: Runtime policy for srt sandboxing.
    :param state_root: Per-call ToolState directory, or ``None``
        for stateless invocations.
    :returns: The wrapped command.
    """
    settings_file = _write_srt_settings_file(state_root) if state_root is not None else None
    return wrap_with_srt(
        cmd,
        sandbox_enabled=sandbox_enabled,
        srt_available=srt_available,
        settings_file=settings_file,
    )


def _build_container_command(sandbox_config: SandboxConfig) -> list[str]:
    """
    Build a container ``run`` command (Docker or Podman).

    The container runs with network disabled, stdin piped,
    and ``_AP_RESPONSE_MODE=stdout`` so the runner writes
    the response to stdout instead of fd 3.

    :param sandbox_config: Agent-level sandbox settings.
    :returns: The container run command list.
    """
    image = sandbox_config.container_image
    assert image is not None, "container_image is required"
    runtime = sandbox_config.container_runtime
    assert runtime is not None, "SandboxConfig must resolve container_runtime"
    return [
        runtime,
        "run",
        "--rm",
        "-i",
        "--network",
        "none",
        "-e",
        "_AP_RESPONSE_MODE=stdout",
        image,
        "python",
        "-c",
        (
            "import sys,json,importlib.util,asyncio,os;"
            "os.environ['_AP_RESPONSE_MODE']='stdout';"
            f"exec(open('{_RUNNER_PATH}').read())"
        ),
    ]


def _write_srt_settings_file(state_root: str) -> str:
    """Write a per-invocation srt settings file for stateful tools.

    The file whitelists ``state_root`` as writable while leaving
    reads unrestricted (empty ``denyRead``), so the subprocess can
    still resolve the venv python and its imports. The settings
    shape matches srt's JSON schema —
    ``network.{allowedDomains,deniedDomains}`` + ``filesystem.
    {denyRead,allowRead,allowWrite,denyWrite}`` (see srt's
    ``SandboxManager`` reference).

    Temp files are intentionally not cleaned up: the payload is
    trivial (~100 bytes), each invocation writes a fresh one, and
    the OS tmpwatch handles eventual garbage collection. Cleaning
    up per-invocation would require another try/finally around
    the subprocess call, which isn't worth it.

    :param state_root: Directory to whitelist as writable inside
        the sandbox.
    :returns: Absolute path to the written settings JSON.
    """
    import tempfile as _tempfile

    settings = {
        "network": {"allowedDomains": [], "deniedDomains": []},
        "filesystem": {
            "denyRead": [],
            "allowRead": [],
            "allowWrite": [state_root],
            "denyWrite": [],
        },
    }
    fd, path = _tempfile.mkstemp(suffix=".srt.json")
    with os.fdopen(fd, "w") as f:
        json.dump(settings, f)
    return path


def _read_fd3_response(
    read_fd: int,
    returncode: int,
    stderr: bytes,
) -> str:
    """
    Read and parse the JSON response from the fd 3 pipe.

    :param read_fd: The read end of the fd 3 pipe.
    :param returncode: The subprocess exit code.
    :param stderr: Captured stderr bytes for error reporting.
    :returns: The tool's result string, or an error string.
    """
    raw = os.read(read_fd, _MAX_RESPONSE_BYTES)
    if not raw:
        stderr_text = stderr.decode(errors="replace").strip()
        if returncode != 0:
            return f"Error: tool subprocess exited with code {returncode}: {stderr_text}"
        return f"Error: tool produced no response. stderr: {stderr_text}"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return f"Error: invalid JSON response from tool: {exc}"

    if "error" in data:
        return f"Error: {data['error']}"
    result: str = data.get("result", "")
    return result


def _read_stdout_response(
    stdout: bytes,
    returncode: int,
    stderr: bytes,
) -> str:
    """
    Read and parse the JSON response from stdout (Docker mode).

    Scans stdout for the ``__AP_RESPONSE__:`` prefix line.

    :param stdout: Captured stdout bytes.
    :param returncode: The subprocess exit code.
    :param stderr: Captured stderr bytes for error reporting.
    :returns: The tool's result string, or an error string.
    """
    if not stdout:
        stderr_text = stderr.decode(errors="replace").strip()
        if returncode != 0:
            return f"Error: tool subprocess exited with code {returncode}: {stderr_text}"
        return f"Error: tool produced no stdout. stderr: {stderr_text}"

    text = stdout.decode(errors="replace")
    for line in text.splitlines():
        if line.startswith(_STDOUT_RESPONSE_PREFIX):
            payload = line[len(_STDOUT_RESPONSE_PREFIX) :]
            try:
                data = json.loads(payload)
            except json.JSONDecodeError as exc:
                return f"Error: invalid JSON response from tool: {exc}"
            if "error" in data:
                return f"Error: {data['error']}"
            return str(data.get("result", ""))

    stderr_text = stderr.decode(errors="replace").strip()
    return (
        f"Error: tool produced no recognized response (no "
        f"{_STDOUT_RESPONSE_PREFIX} prefix found). "
        f"exit={returncode} stderr={stderr_text!r}"
    )


def _discover_tool_metadata(
    *,
    agent_name: str,
    info: LocalToolInfo,
    tool_path: Path,
    sandbox_config: SandboxConfig,
    srt_available: bool,
    uv_available: bool,
    sandbox_enabled: bool,
) -> list[tuple[str, ToolMetadata, bool]]:
    """Discover tool metadata in the same sandbox tier used for invocation."""
    request = json.dumps({"mode": "discover", "module_path": str(tool_path)}).encode()
    cmd = _build_runner_command(
        info=info,
        sandbox_config=sandbox_config,
        srt_available=srt_available,
        uv_available=uv_available,
        sandbox_enabled=sandbox_enabled,
        state_root=None,
    )
    use_stdout = _uses_stdout_protocol(
        sandbox_config,
        srt_available,
        sandbox_enabled,
    )
    try:
        payload, returncode, stderr = _run_discovery_subprocess(
            cmd,
            request,
            use_stdout=use_stdout,
        )
    except subprocess.TimeoutExpired:
        raise LocalToolLoadError(
            f"Agent {agent_name!r}: tool file {tool_path} did not finish "
            f"importing within {int(_DISCOVERY_TIMEOUT_SECONDS)}s during discovery."
        ) from None
    except OSError as exc:
        raise LocalToolLoadError(
            f"Agent {agent_name!r}: cannot start tool discovery subprocess for {tool_path}: {exc}"
        ) from exc
    data = _parse_discovery_payload(
        payload,
        returncode,
        stderr,
        agent_name=agent_name,
        tool_path=tool_path,
    )
    if "error" in data:
        raise LocalToolLoadError(
            f"Agent {agent_name!r}: failed to import tool file {tool_path}: {data['error']}"
        )
    tools = data.get("tools")
    if not isinstance(tools, list):
        raise LocalToolLoadError(
            f"Agent {agent_name!r}: failed to import tool file {tool_path}: "
            "invalid discovery response: missing tools list"
        )
    discovered = [
        (
            tool["name"],
            ToolMetadata(
                name=tool["name"],
                description=tool["description"],
                json_schema=tool["json_schema"],
                strict=tool["strict"],
                return_annotation=None,
                uses_tool_state=tool["uses_tool_state"],
            ),
            tool["uses_tool_state"],
        )
        for tool in tools
    ]
    if discovered:
        return discovered
    raise LocalToolLoadError(
        f"Agent {agent_name!r}: tool file {tool_path} exports no "
        f"@tool-decorated functions. Decorate at least one module-level "
        f"function with @tool from omnigent.tools."
    )


def _run_discovery_subprocess(
    cmd: list[str],
    request: bytes,
    *,
    use_stdout: bool,
) -> tuple[bytes | None, int, bytes]:
    """Run a discovery request using the selected response protocol."""
    env = dict(strip_runner_auth_secrets(os.environ))
    if use_stdout:
        env["_AP_RESPONSE_MODE"] = "stdout"
        return _run_discovery_stdout(cmd, request, env)
    return _run_discovery_fd3(cmd, request, env)


def _run_discovery_stdout(
    cmd: list[str],
    request: bytes,
    env: dict[str, str],
) -> tuple[bytes | None, int, bytes]:
    """Run discovery through the stdout response protocol."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = proc.communicate(
            input=request,
            timeout=_DISCOVERY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    return _read_discovery_stdout_payload(stdout), proc.returncode, stderr


def _run_discovery_fd3(
    cmd: list[str],
    request: bytes,
    env: dict[str, str],
) -> tuple[bytes | None, int, bytes]:
    """Run discovery through the fd 3 response protocol."""
    read_fd, write_fd = os.pipe()
    env["_AP_RESPONSE_FD"] = str(write_fd)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(write_fd,),
            env=env,
        )
        os.close(write_fd)
        write_fd = -1
        try:
            _stdout, stderr = proc.communicate(
                input=request,
                timeout=_DISCOVERY_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        return os.read(read_fd, _MAX_RESPONSE_BYTES) or None, proc.returncode, stderr
    finally:
        if write_fd != -1:
            os.close(write_fd)
        os.close(read_fd)


def _read_discovery_stdout_payload(stdout: bytes) -> bytes | None:
    """Extract a runner response from stdout mixed with tool output."""
    for line in stdout.decode(errors="replace").splitlines():
        if line.startswith(_STDOUT_RESPONSE_PREFIX):
            return line[len(_STDOUT_RESPONSE_PREFIX) :].encode()
    return None


def _parse_discovery_payload(
    payload: bytes | None,
    returncode: int,
    stderr: bytes,
    *,
    agent_name: str,
    tool_path: Path,
) -> dict[str, Any]:
    """Parse a discovery response and turn transport failures into load errors."""
    if not payload:
        stderr_text = stderr.decode(errors="replace").strip()
        raise LocalToolLoadError(
            f"Agent {agent_name!r}: failed to import tool file {tool_path}: "
            f"tool discovery subprocess exited with code {returncode}: "
            f"{stderr_text}"
        )
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise LocalToolLoadError(
            f"Agent {agent_name!r}: failed to import tool file {tool_path}: "
            f"invalid discovery response: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise LocalToolLoadError(
            f"Agent {agent_name!r}: failed to import tool file {tool_path}: "
            "invalid discovery response: expected an object"
        )
    return data


def load_local_python_tools(
    local_tools: list[LocalToolInfo],
    workdir: Path,
    sandbox_config: SandboxConfig | None = None,
    srt_available: bool | None = None,
    uv_available: bool | None = None,
    sandbox_enabled: bool = True,
    *,
    agent_name: str | None = None,
    builtin_tool_names: frozenset[str] | None = None,
) -> list[LocalPythonTool]:
    """
    Load and validate local Python tools from the agent image.

    Each file is imported once in a sandboxed subprocess during
    agent-image load time. Every ``@tool``-decorated function in the module produces one
    :class:`LocalPythonTool`. Names are validated against any
    builtin names provided (collisions fail loud per G27) and
    against each other (two custom tools sharing a name across
    files fail loud).

    :param local_tools: Discovered :class:`LocalToolInfo` entries
        from the agent spec parser (one per file).
    :param workdir: The agent image's extracted directory on disk,
        e.g. ``Path("/tmp/agent-cache/ag_abc123")``.
    :param sandbox_config: Sandbox settings. ``None`` uses defaults.
    :param srt_available: Whether ``srt`` is on PATH. ``None``
        auto-detects.
    :param uv_available: Whether ``uv`` is on PATH. ``None``
        auto-detects.
    :param sandbox_enabled: Runtime policy for srt sandboxing.
    :param agent_name: The agent's name, used in error messages.
        ``None`` falls back to the workdir basename.
    :param builtin_tool_names: Names of framework-provided built-in
        tools enabled for this agent. Used for collision detection
        (G27). ``None`` means skip the builtin-collision check
        (caller already validated, or no builtins active).
    :returns: List of successfully loaded :class:`LocalPythonTool`
        instances, one per ``@tool`` function across all files.
    :raises LocalToolLoadError: If any file fails to load (import
        error, no decorated functions, name collision).
    """
    effective_sandbox = sandbox_config or SandboxConfig()
    effective_srt = srt_available if srt_available is not None else shutil.which("srt") is not None
    effective_uv = uv_available if uv_available is not None else shutil.which("uv") is not None
    effective_agent_name = agent_name or workdir.name

    # Discover decorated functions per file. Track tool name -> source so
    # we can detect cross-file collisions and surface them with both paths.
    discovered: dict[str, _DiscoveredTool] = {}

    for info in local_tools:
        if info.language != "python" or info.runtime != ToolRuntime.SERVER:
            continue
        path = info.path
        if path is None:
            raise LocalToolLoadError(
                f"Agent {effective_agent_name!r}: server tool {info.name!r} has no source path."
            )
        tool_path = Path(path)
        if not tool_path.is_absolute():
            tool_path = workdir / tool_path
        if not tool_path.is_file():
            raise LocalToolLoadError(
                f"Agent {effective_agent_name!r}: tool file declared at "
                f"{info.path!r} but not found on disk."
            )

        # Scan for PEP 723 inline metadata before loading the module.
        _scan_inline_metadata(info, tool_path)

        functions = _discover_tool_metadata(
            agent_name=effective_agent_name,
            info=info,
            tool_path=tool_path,
            sandbox_config=effective_sandbox,
            srt_available=effective_srt,
            uv_available=effective_uv,
            sandbox_enabled=sandbox_enabled,
        )

        for tool_name, metadata, uses_tool_state in functions:
            # Detect collision with another custom tool already discovered.
            existing = discovered.get(tool_name)
            if existing is not None:
                raise LocalToolLoadError(
                    f"Tool name collision in agent {effective_agent_name!r}: "
                    f"'{tool_name}' is defined in both "
                    f"{existing.info.path!r} and {info.path!r}. "
                    f"Rename one of the @tool functions so each name is unique."
                )
            # Detect collision with a builtin.
            if builtin_tool_names is not None and tool_name in builtin_tool_names:
                raise LocalToolLoadError(
                    f"Tool name collision in agent {effective_agent_name!r}: "
                    f"custom tool '{tool_name}' (defined in {info.path!r}) "
                    f"conflicts with built-in tool '{tool_name}'. "
                    f"Rename the custom tool or remove the conflicting builtin "
                    f"from config.yaml's tools.builtins list."
                )
            discovered[tool_name] = _DiscoveredTool(
                info=info,
                metadata=metadata,
                module_path=tool_path.resolve(),
                uses_tool_state=uses_tool_state,
            )

    return [
        LocalPythonTool(
            info=disc.info,
            metadata=disc.metadata,
            module_path=disc.module_path,
            sandbox_config=effective_sandbox,
            srt_available=effective_srt,
            uv_available=effective_uv,
            sandbox_enabled=sandbox_enabled,
            uses_tool_state=disc.uses_tool_state,
        )
        for disc in discovered.values()
    ]


class _DiscoveredTool:
    """
    Internal record produced during loader discovery, before the
    final :class:`LocalPythonTool` instances are constructed.

    Lives only inside :func:`load_local_python_tools`; not part
    of the public API.

    :param info: The :class:`LocalToolInfo` for the source file.
    :param metadata: The :class:`ToolMetadata` from the ``@tool``
        decoration.
    :param module_path: Resolved absolute path to the source file.
    """

    __slots__ = ("info", "metadata", "module_path", "uses_tool_state")

    def __init__(
        self,
        info: LocalToolInfo,
        metadata: ToolMetadata,
        module_path: Path,
        uses_tool_state: bool,
    ) -> None:
        self.info = info
        self.metadata = metadata
        self.module_path = module_path
        self.uses_tool_state = uses_tool_state


def _scan_inline_metadata(info: LocalToolInfo, path: Path) -> None:
    """
    Scan a tool file for PEP 723 inline script metadata.

    Mutates ``info.has_inline_deps`` and ``info.inline_deps``
    in place if dependencies are found.

    :param info: The :class:`LocalToolInfo` to update.
    :param path: Path to the Python file.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return
    metadata = parse_inline_metadata(source)
    if metadata is not None:
        info.has_inline_deps = True
        info.inline_deps = metadata.dependencies
