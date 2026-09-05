"""E2E regression test: pi-native cold resume must not rewrite an explicit
provider-qualified model selection through the configured ``default: pi``
provider.

Reproduces the user-reported bug: a Pi-native
session authenticated with Pi's own ``openai-codex`` provider and pinned to
``openai-codex/gpt-5.6-sol`` works initially, but after the session goes idle
and its terminal is cold-resumed, Omnigent re-resolves the launch and routes
the saved ``openai-codex/gpt-5.6-sol`` selection through the configured
``default: pi`` Omnigent provider (an OpenRouter gateway here). The relaunch
therefore launches Pi with::

    --provider omnigent --model omnigent/openai-codex/gpt-5.6-sol

and the generated ``models.json`` exposes only that single malformed entry, so:

* the next message fails with ``Pi model error: 400: {"message":
  "openai-codex/gpt-5.6-sol is not a valid model ID","code":400}`` (the
  ``omnigent/...`` alias reaches the wire), and
* the web model picker contains only ``omnigent/openai-codex/gpt-5.6-sol`` (the
  live model catalog the extension reports is that single mangled entry), so
  the model can no longer be changed from the UI.

Root cause (for the fix step, not asserted here): on cold resume the runner
calls ``resolve_pi_native_provider(model=model_override)`` with the persisted
explicit selection ``openai-codex/gpt-5.6-sol``. Because ``openai-codex`` is
not one of Omnigent's managed provider ids, ``_split_pi_native_model_selection``
returns ``None`` and the selection is funneled through the ``default: pi``
provider (``_inline_family_pi_provider``), which rebuilds it under the generated
``omnigent`` provider. A fresh launch (``model=None``) instead returns ``None``
and lets Pi use its own login -- which is why it "works initially" and only
breaks on the relaunch that carries the explicit selection.

This test drives the REAL user journey through the real launch orchestration: a
real ``omnigent server`` subprocess, a real runner subprocess whose
``~/.omnigent/config.yaml`` configures an OpenRouter gateway provider with
``default: pi``, a real pi-native wrapper session whose persisted
``model_override`` is the user's explicit ``openai-codex/gpt-5.6-sol``
selection, and then the cold resume the web UI / a daemon relaunch performs --
binding the session to the runner, which auto-creates the Pi terminal and makes
the launch decision under test.

The Pi CLI itself is replaced with a tiny stub that records its argv (the
launch decision the bug corrupts) and parks, so the test needs no ``openai-codex``
OAuth and asserts on the exact seam where the model is rewritten. On the buggy
build the recorded argv contains ``omnigent/openai-codex/gpt-5.6-sol``, so this
test FAILS with the launched argv in the message. The desired behavior is that
the explicit selection survives unchanged (or Pi falls back to its own login),
never the generated ``omnigent/`` alias.

The provider-resolution logic itself is additionally unit-pinned in
``tests/test_pi_native_credentials.py``.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_pi_native_cold_resume_default_provider_rewrite_e2e.py -v
"""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# CI shells can carry an egress proxy in the environment; every HTTP call in
# this test targets 127.0.0.1, so bypass proxy autodetection entirely.
_http = httpx.Client(trust_env=False)

# The runner imports ``omnigent_client`` / ``omnigent_ui_sdk``; in a worktree
# they resolve from sdks/, in an installed venv from site-packages.
_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

# The user's explicit provider-qualified selection: a model that lives ONLY on
# Pi's own ``openai-codex`` provider (OAuth login), NOT on the configured
# Omnigent ``default: pi`` provider. This is the exact id from the bug report.
_EXPLICIT_MODEL = "openai-codex/gpt-5.6-sol"
# The malformed model the buggy cold resume rewrites the selection into by
# routing it through the generated ``omnigent`` provider.
_MANGLED_MODEL = "omnigent/openai-codex/gpt-5.6-sol"

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 1.0
# Terminal auto-create includes bridge prep + tmux boot; generous for CI.
_ARGV_TIMEOUT_S = 180.0

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="pi-native terminals run inside tmux; tmux not installed",
)


def _find_free_port() -> int:
    """Grab an ephemeral port for the spawned server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Subprocess env with worktree imports and no proxy in the way.

    :param extra: Overrides/additions applied after the base env.
    :returns: Environment mapping for ``subprocess.Popen``.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        # CI shells often carry an egress proxy; localhost must bypass it.
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    env.update(extra)
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    """Best-effort SIGTERM -> SIGKILL teardown for a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    """Poll *url* until it returns 200 or *deadline* (monotonic) passes."""
    last = "not polled"
    while time.monotonic() < deadline:
        try:
            if _http.get(url, timeout=2.0).status_code == 200:
                return
            last = "non-200"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(_POLL_S)
    raise AssertionError(f"{url} never became healthy: {last}")


def _create_pi_native_session(base_url: str) -> str:
    """Create a pi-native wrapper session exactly like ``omnigent pi``.

    Reuses the production spec materializer and stamps the same wrapper /
    terminal-first labels the CLI writes, so the runner's pi-native
    auto-bootstrap recognizes the session on bind.

    :param base_url: Spawned server base URL.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        PI_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.pi_native import _materialize_pi_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_pi_agent_spec(Path(tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname routes through the omnigent compat
        # translator (the wrapper spec has no ``spec_version``).
        info = tarfile.TarInfo("pi-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: PI_NATIVE_WRAPPER_VALUE,
    }
    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={
            "bundle": (
                "pi-native-ui.tar.gz",
                buf.getvalue(),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _extract_model_arg(argv: list[str]) -> str | None:
    """Return the value passed to ``--model`` in *argv*, or ``None``.

    :param argv: The recorded Pi launch argv.
    :returns: The ``--model`` value (both ``--model X`` and ``--model=X``
        forms), or ``None`` when no ``--model`` was passed.
    """
    for index, arg in enumerate(argv):
        if arg == "--model" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--model="):
            return arg.split("=", 1)[1]
    return None


def test_cold_resume_preserves_explicit_provider_qualified_model(
    tmp_path: Path,
) -> None:
    """Cold resume must not funnel an explicit selection through ``default: pi``.

    Journey (the reporter's): a Pi-native session pinned to
    ``openai-codex/gpt-5.6-sol`` (a model served only by Pi's own openai-codex
    login) exists, while the runner's config declares an OpenRouter gateway
    provider with ``default: pi``. The user resumes the idled session (relaunch
    -> the runner auto-creates the Pi terminal, re-resolving the launch).

    Expected: the explicit provider-qualified selection survives unchanged --
    Pi is launched with ``--model openai-codex/gpt-5.6-sol`` (or falls back to
    its own login), never rewritten into the generated ``omnigent`` namespace.
    Buggy behavior: the saved selection is routed through the ``default: pi``
    provider and Pi is launched with ``--model omnigent/openai-codex/gpt-5.6-sol``,
    which OpenRouter 400s and which corrupts the web model picker to that single
    malformed entry.

    :param tmp_path: Per-test temp dir (server DB, stub pi, runner HOME/config).
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "chat.db"
    database_uri = f"sqlite:///{db_path}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner_home = tmp_path / "home"
    runner_home.mkdir()

    # The runner's Omnigent config: an OpenRouter gateway provider marked
    # ``default: pi`` -- the exact conflicting-default setup from the report.
    # A fresh launch (model=None) resolves to None here (Pi uses its own
    # login -> "works initially"); a launch carrying the explicit selection
    # is what the bug rewrites.
    config_home = tmp_path / "omnigent_config"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        "providers:\n"
        "  openrouter:\n"
        "    kind: gateway\n"
        "    default: pi\n"
        "    openai:\n"
        "      base_url: https://openrouter.ai/api/v1\n"
        "      api_key: sk-or-testkey\n"
        "      wire_api: chat\n"
        "      model: openai/gpt-4o-mini\n",
        encoding="utf-8",
    )

    # Stub Pi CLI: answers the ``pi --version`` probe (so ``--approve`` is
    # decided normally), otherwise records its argv (the launch decision under
    # test) and parks so the tmux pane stays alive. No openai-codex login
    # needed -- the assertion is on the launch decision, not a live turn.
    argv_file = tmp_path / "pi_argv.txt"
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "pi"
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "0.84.2"; exit 0; fi\n'
        f"{{ printf '%s\\037' \"$@\"; printf '\\n'; }} >> \"{argv_file}\"\n"
        "exec sleep 600\n"
    )
    stub.chmod(0o755)

    def _terminal_launch_argv() -> list[str] | None:
        """The recorded Pi terminal launch argv, once written.

        :returns: The argv of the first recorded non-version invocation, or
            ``None`` if nothing has been recorded yet.
        """
        if not argv_file.exists():
            return None
        for line in argv_file.read_text().splitlines():
            argv = line.split("\x1f")[:-1]
            if argv and argv != ["--version"]:
                return argv
        return None

    binding_token = secrets.token_urlsafe(32)
    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    server_log = (tmp_path / "server.log").open("w")
    runner_log = (tmp_path / "runner.log").open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                database_uri,
                "--artifact-location",
                str(tmp_path / "artifacts"),
            ],
            env=_localhost_env({"OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)

        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=_localhost_env(
                {
                    "OMNIGENT_RUNNER_ID": runner_id,
                    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                    "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                    "RUNNER_SERVER_URL": base_url,
                    "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
                    # Hermetic HOME + config home: the pi-native provider
                    # resolution reads ``~/.omnigent/config.yaml`` (respecting
                    # OMNIGENT_CONFIG_HOME), and the managed Pi agent dir lands
                    # under the bridge dir -- keep both off the real HOME.
                    "HOME": str(runner_home),
                    "OMNIGENT_CONFIG_HOME": str(config_home),
                    # The stub shadows any real pi; OMNIGENT_PI_PATH pins it so
                    # resolve_pi_executable selects the stub deterministically.
                    "OMNIGENT_PI_PATH": str(stub),
                    "PATH": f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                }
            ),
            stdout=runner_log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            try:
                status = _http.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2.0)
                if status.status_code == 200 and status.json().get("online") is True:
                    online = True
                    break
            except httpx.HTTPError:
                # The server/runner is still booting; transient connection
                # errors are expected while polling and simply retried.
                pass
            time.sleep(_POLL_S)
        assert online, (
            f"runner never came online; log:\n{(tmp_path / 'runner.log').read_text()[-3000:]}"
        )

        # A prior Pi-native session pinned to the user's explicit
        # provider-qualified model -- the persisted state a web-picker
        # selection of openai-codex/gpt-5.6-sol produces (model_override),
        # i.e. the state a user cold-resumes into.
        session_id = _create_pi_native_session(base_url)
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"model_override": _EXPLICIT_MODEL},
            timeout=10.0,
        ).raise_for_status()

        # Sanity: the explicit selection persisted verbatim on the session.
        got = _http.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        got.raise_for_status()
        assert got.json().get("model_override") == _EXPLICIT_MODEL, (
            "explicit model_override did not persist on the session"
        )

        # THE RESUME: bind the session to the runner (what the web UI / a
        # daemon relaunch does) -> the runner auto-creates the Pi terminal,
        # re-resolving the launch and deciding the --provider/--model args.
        # The bind can block on runner-side terminal bring-up, so give it the
        # same generous budget as the argv wait.
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=_ARGV_TIMEOUT_S,
        ).raise_for_status()

        deadline = time.monotonic() + _ARGV_TIMEOUT_S
        argv: list[str] | None = None
        while time.monotonic() < deadline:
            argv = _terminal_launch_argv()
            if argv is not None:
                break
            time.sleep(_POLL_S)
        assert argv is not None, (
            f"pi terminal never launched; runner log:\n"
            f"{(tmp_path / 'runner.log').read_text()[-3000:]}"
        )

        # The bug: the cold resume routes the explicit openai-codex selection
        # through the ``default: pi`` provider, launching Pi with the mangled
        # ``omnigent/openai-codex/gpt-5.6-sol`` -- which OpenRouter 400s and
        # which corrupts the model picker. The explicit selection must survive.
        model_arg = _extract_model_arg(argv)
        assert _MANGLED_MODEL not in argv, (
            "cold resume rewrote the explicit provider-qualified selection "
            f"{_EXPLICIT_MODEL!r} into the generated omnigent namespace "
            f"{_MANGLED_MODEL!r} by routing it through the configured "
            f"'default: pi' provider -- the alias reaches the wire and 400s, "
            f"and the picker shows only that malformed entry. launched argv: {argv}"
        )
        if model_arg is not None:
            assert model_arg == _EXPLICIT_MODEL, (
                "cold resume must launch Pi with the byte-identical explicit "
                f"selection {_EXPLICIT_MODEL!r} (or fall back to Pi's own "
                f"login), not {model_arg!r}. launched argv: {argv}"
            )
    finally:
        _terminate(runner_proc)
        _terminate(server_proc)
        server_log.close()
        runner_log.close()
