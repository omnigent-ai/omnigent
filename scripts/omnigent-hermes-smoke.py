#!/usr/bin/env python3
"""Neutral-cwd smoke for the installed local Omnigent/Hermes stack.

The script starts from this checkout but re-execs into the uv-tool Python named
by the installed ``omni`` entrypoint. That keeps the proof grounded in the
installed working setup instead of importing this source tree.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_OMNI = Path("/Users/spencer/.local/bin/omni")
DEFAULT_HERMES = Path("/Users/spencer/.local/bin/hermes")
REEXEC_ENV = "OMNIGENT_HERMES_SMOKE_REEXEC"


def _resolve_executable(env_name: str, default_path: Path, *names: str) -> Path:
    requested = os.environ.get(env_name, "").strip()
    if requested:
        path = Path(requested).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
        raise SystemExit(f"ERROR: {env_name}={requested} is not executable")

    if default_path.is_file() and os.access(default_path, os.X_OK):
        return default_path.resolve()

    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return Path(resolved).resolve()
    raise SystemExit(f"ERROR: could not find any of: {', '.join(names)}")


def _entrypoint_python(entrypoint: Path) -> Path:
    try:
        first = entrypoint.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError) as exc:
        raise SystemExit(f"ERROR: could not read entrypoint shebang from {entrypoint}") from exc
    if not first.startswith("#!"):
        raise SystemExit(f"ERROR: {entrypoint} does not have a Python shebang")
    python = Path(first[2:].strip().split()[0]).expanduser()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise SystemExit(f"ERROR: entrypoint Python is not executable: {python}")
    # Do not resolve the venv's python symlink. CPython detects the uv tool
    # environment from the executable path; resolving to the shared base
    # interpreter drops the tool site-packages.
    return python


def _reexec_into_installed_python(omni_bin: Path) -> None:
    installed_python = _entrypoint_python(omni_bin)
    if Path(sys.executable).resolve() == installed_python:
        return
    env = os.environ.copy()
    env[REEXEC_ENV] = "1"
    env["OMNIGENT_CLI_RESOLVED"] = str(omni_bin)
    os.execve(str(installed_python), [str(installed_python), __file__, *sys.argv[1:]], env)


def _neutralize_source_checkout() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    neutral_cwd = Path(
        os.environ.get("OMNIGENT_HERMES_SMOKE_CWD", tempfile.gettempdir())
    ).resolve()
    neutral_cwd.mkdir(parents=True, exist_ok=True)
    os.chdir(neutral_cwd)

    cleaned: list[str] = []
    for entry in sys.path:
        if entry == "":
            continue
        try:
            if Path(entry).resolve() == repo_root:
                continue
        except OSError:
            pass
        cleaned.append(entry)
    sys.path[:] = cleaned
    return neutral_cwd


def _run_direct_hermes_check(
    hermes_bin: Path,
    neutral_cwd: Path,
    marker: str,
    timeout: int,
) -> None:
    prompt = f"Reply with exactly: {marker}"
    proc = subprocess.run(
        [str(hermes_bin), "chat", "-q", prompt, "-Q", "--source", "tool"],
        cwd=neutral_cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            "ERROR: direct Hermes one-shot failed "
            f"(exit {proc.returncode}): {proc.stderr.strip()[:800]}"
        )
    if marker not in proc.stdout:
        raise SystemExit(
            "ERROR: direct Hermes one-shot did not return the marker. "
            f"stdout={proc.stdout.strip()[:800]!r}"
        )
    print(f"direct_hermes: PASS marker={marker}")


def _assistant_text(item: dict[str, object]) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks)


async def _run_native_smoke(
    *,
    server: str | None,
    timeout: int,
    keep_session: bool,
    neutral_cwd: Path,
    marker: str,
) -> None:
    import httpx

    from omnigent.chat import _bundle_agent, _remote_headers
    from omnigent.cli import _ensure_backend
    from omnigent.hermes_native import (
        _materialize_hermes_agent_spec,
        _prepare_hermes_terminal_via_daemon,
    )
    from omnigent.host.identity import load_or_create_host_identity
    from omnigent.host.local_server import local_server_url_if_healthy

    base_url = (server or _ensure_backend(None)).rstrip("/")
    host_id = load_or_create_host_identity().host_id
    candidates = [base_url]
    if server is None:
        current_local = local_server_url_if_healthy()
        if current_local:
            candidates.append(current_local.rstrip("/"))
    base_url = await _select_online_host_url(
        candidates=candidates,
        host_id=host_id,
        timeout=float(timeout),
        refresh_local=server is None,
    )
    headers = _remote_headers(server_url=base_url)
    session_id: str | None = None

    workspace = Path(tempfile.mkdtemp(prefix="omnigent-hermes-smoke-ws-", dir=neutral_cwd))
    try:
        print(f"host_daemon: PASS host_id={host_id}")

        with tempfile.TemporaryDirectory(
            prefix="omnigent-hermes-smoke-spec-",
            dir=neutral_cwd,
        ) as td:
            spec_path = _materialize_hermes_agent_spec(Path(td))
            bundle = _bundle_agent(spec_path)
            prepared = await _prepare_hermes_terminal_via_daemon(
                base_url=base_url,
                headers=headers,
                session_id=None,
                session_bundle=bundle,
                hermes_args=(),
                host_id=host_id,
                workspace=str(workspace),
                startup_progress=None,
            )
        session_id = prepared.session_id

        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30.0) as client:
            health = await client.get("/health")
            health.raise_for_status()

            agent = (await client.get(f"/v1/sessions/{session_id}/agent")).json()
            name = agent.get("name")
            executor = agent.get("executor")
            harness = (
                executor.get("harness") if isinstance(executor, dict) else agent.get("harness")
            )
            if name != "hermes-native-ui" or harness != "hermes-native":
                raise SystemExit(
                    "ERROR: session did not materialize hermes-native-ui "
                    f"(name={name!r}, harness={harness!r})"
                )
            print(f"native_agent: PASS session={session_id} name={name} harness={harness}")

            event = {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"Reply with exactly: {marker}"}],
                },
            }
            post = await client.post(f"/v1/sessions/{session_id}/events", json=event)
            post.raise_for_status()

            deadline = time.monotonic() + timeout
            last_reply = ""
            while time.monotonic() < deadline:
                items = await client.get(
                    f"/v1/sessions/{session_id}/items",
                    params={"limit": 100, "order": "asc"},
                    timeout=10.0,
                )
                if items.status_code == 200:
                    for item in items.json().get("data", []):
                        if item.get("type") == "message" and item.get("role") == "assistant":
                            text = _assistant_text(item)
                            if text:
                                last_reply = text
                                if marker in text:
                                    print(f"native_reply: PASS marker={marker}")
                                    return
                await asyncio.sleep(2.0)
            raise SystemExit(
                "ERROR: hermes-native-ui did not answer with the marker "
                f"within {timeout}s. last_reply={last_reply[:800]!r}"
            )
    finally:
        if session_id and not keep_session:
            try:
                import httpx

                async with httpx.AsyncClient(
                    base_url=base_url,
                    headers=headers,
                    timeout=10.0,
                ) as c:
                    await c.delete(f"/v1/sessions/{session_id}")
                print(f"cleanup: deleted session={session_id}")
            except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
                print(
                    f"cleanup: WARN could not delete session={session_id}: {exc}",
                    file=sys.stderr,
                )
        shutil.rmtree(workspace, ignore_errors=True)


async def _select_online_host_url(
    *,
    candidates: list[str],
    host_id: str,
    timeout: float,
    refresh_local: bool,
) -> str:
    import httpx

    from omnigent.chat import _remote_headers
    from omnigent.host.local_server import local_server_url_if_healthy

    seen: list[str] = []

    def add_candidate(url: str | None) -> None:
        if not url:
            return
        clean = url.rstrip("/")
        if clean not in seen:
            seen.append(clean)

    for candidate in candidates:
        add_candidate(candidate)

    deadline = time.monotonic() + timeout
    last_status: dict[str, str] = {}
    while time.monotonic() < deadline:
        if refresh_local:
            add_candidate(local_server_url_if_healthy())
        for candidate in list(seen):
            try:
                async with httpx.AsyncClient(
                    base_url=candidate,
                    headers=_remote_headers(server_url=candidate),
                    timeout=5.0,
                ) as client:
                    hosts = await client.get("/v1/hosts")
                    hosts.raise_for_status()
                    for host in hosts.json().get("hosts", []):
                        if host.get("host_id") == host_id:
                            status = str(host.get("status"))
                            last_status[candidate] = status
                            if status == "online":
                                print(f"host_server: PASS base_url={candidate}")
                                return candidate
            except Exception as exc:  # noqa: BLE001 - reported below as diagnostics
                last_status[candidate] = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(1.0)

    details = ", ".join(f"{url} -> {status}" for url, status in last_status.items())
    raise SystemExit(
        "ERROR: local host daemon did not come online for any candidate server "
        f"within {timeout:.0f}s. {details}"
    )


def _check_model_endpoint(timeout: float) -> None:
    import httpx

    try:
        resp = httpx.get("http://127.0.0.1:8080/v1/models", timeout=timeout)
        resp.raise_for_status()
        models = [m.get("id") for m in resp.json().get("data", []) if isinstance(m, dict)]
    except Exception as exc:
        raise SystemExit(f"ERROR: local model endpoint is not reachable: {exc}") from exc
    if "qwen3-coder-next-local" not in models:
        raise SystemExit(f"ERROR: qwen3-coder-next-local missing from /v1/models: {models!r}")
    print("local_model: PASS model=qwen3-coder-next-local base_url=http://127.0.0.1:8080/v1")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", help="Omnigent server URL. Defaults to managed local mode.")
    parser.add_argument("--timeout", type=int, default=240, help="Native reply timeout seconds.")
    parser.add_argument(
        "--direct-timeout",
        type=int,
        default=120,
        help="Direct Hermes one-shot timeout seconds.",
    )
    parser.add_argument(
        "--skip-direct",
        action="store_true",
        help="Skip the direct `hermes chat` lower-bound check.",
    )
    parser.add_argument(
        "--keep-session",
        action="store_true",
        help="Leave the smoke session in Omnigent instead of deleting it.",
    )
    args = parser.parse_args()

    omni_bin = _resolve_executable("OMNIGENT_CLI", DEFAULT_OMNI, "omni", "omnigent")
    hermes_bin = _resolve_executable("OMNIGENT_HERMES_PATH", DEFAULT_HERMES, "hermes")
    os.environ.setdefault("OMNIGENT_HERMES_PATH", str(hermes_bin))
    os.environ["PATH"] = f"{omni_bin.parent}:{hermes_bin.parent}:{os.environ.get('PATH', '')}"

    if os.environ.get(REEXEC_ENV) != "1":
        _reexec_into_installed_python(omni_bin)

    neutral_cwd = _neutralize_source_checkout()
    print(f"neutral_cwd: {neutral_cwd}")
    print(f"omnigent_cli: {omni_bin}")
    print(f"hermes_cli: {hermes_bin}")
    print(f"omnigent_package: {importlib.metadata.version('omnigent')}")

    _check_model_endpoint(timeout=5.0)
    if not args.skip_direct:
        _run_direct_hermes_check(
            hermes_bin,
            neutral_cwd,
            "OMNIGENT_HERMES_DIRECT_OK",
            args.direct_timeout,
        )

    asyncio.run(
        _run_native_smoke(
            server=args.server,
            timeout=args.timeout,
            keep_session=args.keep_session,
            neutral_cwd=neutral_cwd,
            marker="OMNIGENT_HERMES_NATIVE_OK",
        )
    )
    print("PASS: installed Omnigent/Hermes native bootstrap is usable from a neutral cwd")


if __name__ == "__main__":
    main()
