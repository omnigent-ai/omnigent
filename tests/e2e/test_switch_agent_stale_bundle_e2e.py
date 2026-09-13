"""E2E: a switch-created agent clone must not pin a stale bundle forever.

Switching a session's agent (``POST /v1/sessions/{id}/switch-agent``) clones
the target built-in into a session-scoped agent row named
``"<name> (switch <id>)"`` whose ``bundle_location`` is copied verbatim. That
row is never touched again: startup ``--agent`` re-registration resolves the
template by name and updates only the template row, so after a bundle change
plus server restart (a redeploy) the clone still points at the old blob.

The user-visible failure: the new-session picker discovers the clone from
the session scan under a near-identical name, and a session started from it
runs the PRE-change spec — old prompt, old policy, old sandbox grants —
with no staleness indication anywhere in the UI.

Journey (all user-observable):

1. Register an agent from a bundle directory (server boots with
   ``--agent <dir>``, bundle content "V1").
2. In a session, switch the agent to it — the ``"<name> (switch <id>)"``
   session-scoped clone row is created.
3. Change the bundle on disk and restart the server so startup
   registration picks the new content up (a redeploy).
4. Start a new session from the switch clone (what the picker binds).
5. The new session must run the CURRENT spec — on the broken build it
   serves the pre-change bundle, silently.

Hermetic: no LLM turn and no runner are needed. The spec a session runs is
exactly what ``GET /v1/sessions/{id}/agent/contents`` serves (the runner
fetches its bundle from that endpoint on cache miss), so asserting on the
served bundle bytes IS asserting on the spec the session executes.

Usage::

    pytest tests/e2e/test_switch_agent_stale_bundle_e2e.py -v
"""

from __future__ import annotations

import gzip
import io
import os
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="boots a POSIX omnigent server subprocess"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SERVER_HEALTH_TIMEOUT_S = 90.0

#: First-party sentinel Origin so JSON POST /v1/sessions passes the
#: require_trusted_origin guard (same choice as tests/e2e/conftest.py).
_ORIGIN = {"Origin": "omnigent://internal"}

#: Content markers baked into the bundle prompt; the assertion greps the
#: served bundle for these, so they must never substring-collide.
_V1_MARKER = "STALE-SPEC-V1"
_V2_MARKER = "CURRENT-SPEC-V2"


def _write_bundle(bundle_dir: Path, marker: str) -> None:
    """(Re)write the ``--agent`` bundle directory in place, like a redeploy.

    :param bundle_dir: The agent-image directory registered at startup.
    :param marker: Version marker carried in the spec's prompt.
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "config.yaml").write_text(
        "spec_version: 1\n"
        "name: pinned-probe\n"
        f'prompt: "You are pinned-probe {marker}."\n'
        "executor:\n"
        "  config:\n"
        "    harness: openai-agents\n"
        f"    model: mock-{marker.lower()}\n"
    )


def _write_source_agent(bundle_dir: Path) -> None:
    """Write the session's FIRST agent (the one switched away from)."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "config.yaml").write_text(
        "spec_version: 1\n"
        "name: switch-source\n"
        'prompt: "You are switch-source."\n'
        "executor:\n"
        "  config:\n"
        "    harness: openai-agents\n"
        "    model: mock-switch-source\n"
    )


def _free_port() -> int:
    """Return an ephemeral loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Rig:
    """A sandboxed, restartable ``omnigent server`` with ``--agent`` dirs.

    Isolated ``OMNIGENT_CONFIG_HOME`` / ``OMNIGENT_DATA_DIR`` and its own
    SQLite DB + artifact store, both of which SURVIVE a restart — the
    restart-with-changed-bundle is the journey under test.
    """

    def __init__(self, root: Path, agent_dirs: list[Path]) -> None:
        self.root = root
        self.agent_dirs = agent_dirs
        self.base_url = ""
        self._server: subprocess.Popen[bytes] | None = None
        self.client = httpx.Client(trust_env=False, timeout=30.0, headers=_ORIGIN)

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key in list(env):
            # An omnigent-managed test session leaks runner/host identity
            # and proxy settings; inherited, they would misroute the rig.
            if key.startswith(("OMNIGENT", "CLAUDECODE", "RUNNER_SERVER_URL")) or key.lower() in (
                "http_proxy",
                "https_proxy",
                "all_proxy",
                "no_proxy",
            ):
                del env[key]
        env.update(
            {
                "OMNIGENT_CONFIG_HOME": str(self.root / "config-home"),
                "OMNIGENT_DATA_DIR": str(self.root / "data"),
                "PYTHONPATH": os.pathsep.join(
                    [
                        str(_REPO_ROOT),
                        str(_REPO_ROOT / "sdks" / "python-client"),
                        str(_REPO_ROOT / "sdks" / "ui"),
                    ]
                ),
            }
        )
        return env

    def start(self) -> None:
        """Boot (or reboot) the rig server and wait for ``/health``."""
        port = _free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        for sub in ("config-home", "data"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        args = [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{self.root / 'rig.db'}",
            "--artifact-location",
            str(self.root / "artifacts"),
        ]
        for agent_dir in self.agent_dirs:
            args.extend(["--agent", str(agent_dir)])
        log = open(self.root / "server.log", "a")  # noqa: SIM115 — subprocess lifetime
        self._server = subprocess.Popen(
            args, env=self._env(), stdout=log, stderr=subprocess.STDOUT
        )
        deadline = time.monotonic() + _SERVER_HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._server.poll() is not None:
                raise RuntimeError(
                    "rig server exited early; log tail:\n"
                    + (self.root / "server.log").read_text(errors="replace")[-3000:]
                )
            try:
                if self.client.get(f"{self.base_url}/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise RuntimeError(
            "rig server /health never came up; log tail:\n"
            + (self.root / "server.log").read_text(errors="replace")[-3000:]
        )

    def stop(self) -> None:
        """Stop the server (the DB and artifact store stay for the reboot)."""
        if self._server is None or self._server.poll() is not None:
            return
        self._server.terminate()
        try:
            self._server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._server.kill()
            self._server.wait(timeout=5)

    def close(self) -> None:
        self.stop()
        self.client.close()

    # ── journey helpers ──────────────────────────────────────────

    def agent_id_by_name(self, name: str) -> str:
        """Template agent id from ``GET /v1/agents`` (what the picker lists)."""
        resp = self.client.get(f"{self.base_url}/v1/agents", params={"limit": 1000})
        resp.raise_for_status()
        for agent in resp.json()["data"]:
            if agent["name"] == name:
                return str(agent["id"])
        raise AssertionError(f"agent {name!r} not registered on the rig server")

    def create_session(self, agent_id: str) -> httpx.Response:
        """``POST /v1/sessions`` by agent id — the picker's bind call."""
        return self.client.post(f"{self.base_url}/v1/sessions", json={"agent_id": agent_id})

    def served_bundle_text(self, session_id: str) -> str:
        """The spec text a runner would execute for *session_id*.

        ``GET /v1/sessions/{id}/agent/contents`` is the endpoint runners
        fetch their bundle from; its bytes ARE the session's effective spec.
        """
        resp = self.client.get(f"{self.base_url}/v1/sessions/{session_id}/agent/contents")
        resp.raise_for_status()
        raw = gzip.decompress(resp.content)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            for member in tar.getmembers():
                if member.name.endswith("config.yaml"):
                    extracted = tar.extractfile(member)
                    assert extracted is not None
                    return extracted.read().decode()
        raise AssertionError("served bundle has no config.yaml")


@pytest.mark.timeout(300)
def test_new_session_from_switch_clone_serves_current_bundle(tmp_path: Path) -> None:
    """A session started from a switch clone must not run a stale spec.

    The ``"<name> (switch <id>)"`` clone
    row pins the bundle_location captured at switch time and is invisible
    to startup ``--agent`` re-registration (which resolves by the CLEAN
    template name). After a bundle change + restart, a new session bound
    to the clone silently serves the pre-change bundle while a session on
    the template serves the current one.

    Passes when a session from the clone serves the CURRENT bundle content
    — the clone tracks its source template across redeploys, and binding a
    clone the caller could already bind keeps working.
    """
    probe_dir = tmp_path / "agents" / "pinned-probe"
    source_dir = tmp_path / "agents" / "switch-source"
    _write_bundle(probe_dir, _V1_MARKER)
    _write_source_agent(source_dir)

    rig = _Rig(tmp_path / "rig", [probe_dir, source_dir])
    try:
        # Step 1 — register both agents at startup (V1 content).
        rig.start()
        probe_id = rig.agent_id_by_name("pinned-probe")
        source_id = rig.agent_id_by_name("switch-source")

        # Step 2 — a session on switch-source, switched in place to
        # pinned-probe. This mints the session-scoped clone row.
        created = rig.create_session(source_id)
        created.raise_for_status()
        session_id = str(created.json()["id"])
        switched = rig.client.post(
            f"{rig.base_url}/v1/sessions/{session_id}/switch-agent",
            json={"agent_id": probe_id},
        )
        switched.raise_for_status()

        bound = rig.client.get(f"{rig.base_url}/v1/sessions/{session_id}/agent")
        bound.raise_for_status()
        clone_id = str(bound.json()["id"])
        clone_name = str(bound.json()["name"])
        assert "(switch" in clone_name, (
            f"setup: expected a switch-clone agent row, got {clone_name!r}"
        )
        assert _V1_MARKER in rig.served_bundle_text(session_id), (
            "setup: the switched session should serve the V1 bundle pre-deploy"
        )

        # Step 3 — the deploy: bundle content changes, server restarts,
        # startup registration updates the template row.
        rig.stop()
        _write_bundle(probe_dir, _V2_MARKER)
        rig.start()

        # Control: a fresh session on the TEMPLATE serves the new content.
        control = rig.create_session(rig.agent_id_by_name("pinned-probe"))
        control.raise_for_status()
        control_text = rig.served_bundle_text(str(control.json()["id"]))
        assert _V2_MARKER in control_text, (
            "setup: post-restart template session should serve the V2 bundle, "
            f"got: {control_text!r}"
        )

        # Steps 4–5 — a NEW session from the switch clone (what the picker
        # binds for the near-identical "(switch …)" row).
        from_clone = rig.create_session(clone_id)
        # Binding the clone must keep working: the fix repoints the clone's
        # bundle rather than making it unbindable, and an unexpected 4xx
        # here would silently regress the picker's session-scan rows.
        from_clone.raise_for_status()
        clone_session_id = str(from_clone.json()["id"])
        served = rig.served_bundle_text(clone_session_id)
        assert _V2_MARKER in served and _V1_MARKER not in served, (
            "a new session started from the switch clone "
            f"({clone_name!r}) still runs the PRE-deploy spec: served bundle "
            f"contains {_V1_MARKER!r} instead of {_V2_MARKER!r}. The clone row "
            "pins the bundle_location captured at switch time and startup "
            "--agent re-registration (which resolves the template by its clean "
            "name) never updates it, so the picker row silently launches an "
            "outdated prompt/policy/sandbox spec."
        )
    finally:
        rig.close()
