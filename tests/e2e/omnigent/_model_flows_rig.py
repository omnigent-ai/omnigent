"""Sandbox rig for the live model-flows CUJs (design: model-flows-design.md §10.1).

Boots a real ``omnigent server`` + ``omnigent host`` from a chosen checkout —
``OMNIGENT_E2E_MODEL_FLOWS_REPO`` selects which, so the identical tests can run
against unmodified main (the red-on-main matrix) and against this branch — with
the developer's real ``$HOME`` (the claude/codex logins cannot be relocated) but
an isolated ``OMNIGENT_CONFIG_HOME`` / ``OMNIGENT_DATA_DIR``. Provider *shapes*
(which provider entry is the default for each model family) are rewritten into
the sandbox config per test group, mirroring how ``omnigent setup`` flips the
``default:`` claims.

Every test drives the product the way a person uses it: the browser drives the
rig server's real SPA (Playwright sync API), and harness truth is read where a
user reads it — the tmux pane and the harness's own on-disk state. REST reads
are secondary probes only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.e2e.routing._helpers import wait_for

#: Opt-in gate: this suite launches real claude/codex TUIs and does real
#: inference, so it never runs in CI by accident.
RUN_GATE_ENV = "OMNIGENT_E2E_MODEL_FLOWS"

#: Checkout to boot the rig from. Defaults to this test file's repo. Point it
#: at an unmodified main checkout to produce the red-on-main matrix; the rig
#: prefers that checkout's own ``.venv`` interpreter so its dependency set
#: matches its code.
RIG_REPO_ENV = "OMNIGENT_E2E_MODEL_FLOWS_REPO"

#: Optional prebuilt SPA dist for the rig server (``OMNIGENT_WEB_UI_DIST``
#: passthrough) — needed when the target checkout's packaged SPA is stale
#: relative to its web/ sources.
SPA_DIST_ENV = "OMNIGENT_E2E_MODEL_FLOWS_WEB_DIST"

_THIS_REPO = Path(__file__).resolve().parents[3]

_SERVER_HEALTH_TIMEOUT_S = 60.0
_HOST_REGISTER_TIMEOUT_S = 60.0
#: Session create → pane visible. Real CLI boots take a while on a cold disk.
PANE_TIMEOUT_S = 90.0


def require_opt_in() -> None:
    """Skip unless the live model-flows suite was asked for explicitly."""
    if os.environ.get(RUN_GATE_ENV) != "1":
        pytest.skip(
            f"set {RUN_GATE_ENV}=1 to run the live model-flow CUJs "
            "(they launch real claude/codex TUIs and a real host daemon)"
        )


def require_clis(*clis: str) -> None:
    """Skip when a needed CLI is not on PATH.

    :param clis: Binary names, e.g. ``"claude"``, ``"codex"``, ``"tmux"``.
    """
    for cli in clis:
        if shutil.which(cli) is None:
            pytest.skip(f"{cli!r} is not on PATH; this CUJ cannot launch")


def rig_repo() -> Path:
    """Return the checkout the rig boots from.

    :returns: Absolute repo root, e.g. ``~/omnigent`` for a red-on-main run.
    """
    override = os.environ.get(RIG_REPO_ENV)
    return Path(override).expanduser().resolve() if override else _THIS_REPO


def _rig_python(repo: Path) -> str:
    """Interpreter for rig subprocesses: the checkout's venv when it has one.

    :param repo: The checkout to boot.
    :returns: Path to a python executable.
    """
    venv_python = repo / ".venv" / "bin" / "python3"
    return str(venv_python) if venv_python.exists() else sys.executable


def developer_providers() -> dict[str, Any]:
    """Read the developer's real ``providers:`` block.

    The live CUJs exercise the machine's actual provider entries (subscription
    logins, the gateway entry, the databricks profile); the sandbox copies the
    block and only flips ``default:`` claims per shape.

    :returns: The ``providers`` mapping from ``~/.omnigent/config.yaml``.
    """
    path = Path.home() / ".omnigent" / "config.yaml"
    if not path.is_file():
        pytest.skip("no ~/.omnigent/config.yaml; the live model-flow CUJs need real providers")
    parsed = yaml.safe_load(path.read_text()) or {}
    providers = parsed.get("providers")
    if not isinstance(providers, dict) or not providers:
        pytest.skip("~/.omnigent/config.yaml has no providers block")
    return {name: dict(body) for name, body in providers.items() if isinstance(body, dict)}


def _entry_of_kind(providers: dict[str, Any], kind: str, *, cli: str | None = None) -> str | None:
    """Find a provider entry name by ``kind`` (and optional ``cli``).

    :param providers: The providers mapping.
    :param kind: e.g. ``"subscription"``, ``"gateway"``, ``"databricks"``.
    :param cli: Restrict subscription entries to this CLI, e.g. ``"claude"``.
    :returns: The entry name, or ``None``.
    """
    for name, body in providers.items():
        if body.get("kind") != kind:
            continue
        if cli is not None and body.get("cli") != cli:
            continue
        return name
    return None


def shape_providers(shape: str) -> dict[str, Any]:
    """Return a providers block with ``default:`` claims flipped for *shape*.

    Shapes mirror the analysis doc's live matrix:

    - ``claude-subscription`` — claude's subscription entry claims anthropic.
    - ``claude-gateway`` — a ``kind: gateway`` entry with an anthropic family
      claims anthropic (the isaac-style hand-written gateway entry).
    - ``codex-databricks`` — the ``kind: databricks`` entry claims openai.
    - ``codex-subscription`` — codex's subscription entry claims openai.

    Skips when the developer's config lacks the entry a shape needs.

    :param shape: One of the four shape names above.
    :returns: A deep-copied providers mapping with defaults rewritten.
    """
    providers = developer_providers()
    # Strip every existing default claim; each shape sets exactly what it needs.
    for body in providers.values():
        body.pop("default", None)

    def _need(name: str | None, what: str) -> str:
        if name is None:
            pytest.skip(f"developer config has no {what}; shape {shape!r} cannot run")
        return name

    if shape == "claude-subscription":
        name = _need(
            _entry_of_kind(providers, "subscription", cli="claude"), "claude subscription entry"
        )
        providers[name]["default"] = True
    elif shape == "claude-gateway":
        gateway = next(
            (
                n
                for n, b in providers.items()
                if b.get("kind") == "gateway" and isinstance(b.get("anthropic"), dict)
            ),
            None,
        )
        name = _need(gateway, "anthropic-family gateway entry")
        providers[name]["default"] = True
    elif shape == "codex-databricks":
        name = _need(_entry_of_kind(providers, "databricks"), "databricks entry")
        providers[name]["default"] = "openai"
    elif shape == "codex-subscription":
        name = _need(
            _entry_of_kind(providers, "subscription", cli="codex"), "codex subscription entry"
        )
        providers[name]["default"] = "openai"
    else:  # pragma: no cover - test-authoring error
        raise ValueError(f"unknown shape {shape!r}")
    return providers


@dataclass
class ModelFlowsRig:
    """A booted sandbox server (+ optionally a host) from one checkout."""

    repo: Path
    root: Path
    base_url: str = ""
    _server: subprocess.Popen[bytes] | None = None
    _host: subprocess.Popen[bytes] | None = None
    _logs: list[Any] = field(default_factory=list)
    host_id: str = ""

    @property
    def server_log(self) -> Path:
        """The rig server's log path."""
        return self.root / "server.log"

    @property
    def host_log(self) -> Path:
        """The rig host's log path."""
        return self.root / "host.log"

    @property
    def data_dir(self) -> Path:
        """The sandbox ``OMNIGENT_DATA_DIR``."""
        return self.root / "data"

    def _env(self) -> dict[str, str]:
        env = {
            **os.environ,
            "OMNIGENT_CONFIG_HOME": str(self.root / "config-home"),
            "OMNIGENT_DATA_DIR": str(self.data_dir),
            "PYTHONPATH": str(self.repo),
            "OMNIGENT_LOG_TO_STDERR": "1",
        }
        # Claude Code refuses nested sessions; the agent driving this suite
        # may export the marker. And when this suite itself runs inside an
        # omnigent-managed session, the inherited runner identity would point
        # spawned runners at the WRONG server — scrub it.
        env.pop("CLAUDECODE", None)
        env.pop("RUNNER_SERVER_URL", None)
        env.pop("OMNIGENT", None)
        for key in [k for k in env if k.startswith(("OMNIGENT_RUNNER", "OMNIGENT_PROCESS"))]:
            env.pop(key, None)
        # tests/conftest.py exports OMNIGENT_DISABLE_CATALOG_LOOKUP=1 for the
        # whole pytest process (hermetic suites must not hit the network). This
        # suite is the OPPOSITE: a live rig whose databricks shapes need the
        # real provider catalog, and the spawned server/host inherit our env —
        # so drop the kill switch for them.
        env.pop("OMNIGENT_DISABLE_CATALOG_LOOKUP", None)
        spa_dist = os.environ.get(SPA_DIST_ENV)
        if spa_dist:
            env["OMNIGENT_WEB_UI_DIST"] = spa_dist
        return env

    def start_server(self) -> None:
        """Boot the rig server on a free port and wait for /health."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        (self.root / "config-home").mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        log_handle = open(self.server_log, "w")  # noqa: SIM115 — subprocess lifetime
        self._logs.append(log_handle)
        self._server = subprocess.Popen(
            [
                _rig_python(self.repo),
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
            ],
            env=self._env(),
            cwd=str(self.repo),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        self.base_url = f"http://127.0.0.1:{port}"

        def _healthy() -> bool | None:
            if self._server is not None and self._server.poll() is not None:
                raise RuntimeError(
                    f"rig server exited early; log tail:\n{self._tail(self.server_log)}"
                )
            try:
                return httpx.get(f"{self.base_url}/health", timeout=2).status_code == 200 or None
            except httpx.HTTPError:
                return None

        wait_for(_healthy, timeout=_SERVER_HEALTH_TIMEOUT_S, what="the rig server /health")

    def start_host(self, providers: dict[str, Any]) -> str:
        """Boot (or reboot) the rig host with *providers*; return its host id.

        :param providers: The sandbox ``providers:`` block for this shape.
        :returns: The registered host id.
        """
        self.stop_host()
        config_home = self.root / "config-home"
        config_home.mkdir(parents=True, exist_ok=True)
        (config_home / "config.yaml").write_text(
            yaml.safe_dump({"providers": providers}, sort_keys=False)
        )
        log_handle = open(self.host_log, "w")  # noqa: SIM115 — subprocess lifetime
        self._logs.append(log_handle)
        self._host = subprocess.Popen(
            [
                _rig_python(self.repo),
                "-m",
                "omnigent.host._daemon_entry",
                "--server",
                self.base_url,
            ],
            env=self._env(),
            cwd=str(self.repo),
            stdout=subprocess.DEVNULL,
            stderr=log_handle,
        )

        def _online() -> str | None:
            if self._host is not None and self._host.poll() is not None:
                raise RuntimeError(
                    f"rig host exited early; log tail:\n{self._tail(self.host_log)}"
                )
            try:
                resp = httpx.get(f"{self.base_url}/v1/hosts", timeout=5)
            except httpx.HTTPError:
                return None
            if resp.status_code != 200:
                return None
            online = [h for h in resp.json().get("hosts", []) if h.get("status") == "online"]
            return str(online[0]["host_id"]) if online else None

        self.host_id = wait_for(
            _online, timeout=_HOST_REGISTER_TIMEOUT_S, what="the rig host to register"
        )
        return self.host_id

    def _tail(self, path: Path, limit: int = 3000) -> str:
        return path.read_text(errors="replace")[-limit:] if path.exists() else ""

    def stop_host(self) -> None:
        """Stop the host subprocess if one is running."""
        self._stop(self._host)
        self._host = None

    def stop(self) -> None:
        """Tear the whole rig down."""
        self.stop_host()
        self._stop(self._server)
        self._server = None
        for handle in self._logs:
            handle.close()
        self._logs.clear()

    @staticmethod
    def _stop(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@contextmanager
def booted_rig(tmp_root: Path) -> Iterator[ModelFlowsRig]:
    """Context manager: a rig with its server booted (no host yet).

    Snapshots the developer's ``~/.claude/settings.json`` and restores it on
    exit: the suite's real ``/model`` switches run under the real ``$HOME``
    and Claude Code persists every switch as the person's global default —
    without the restore, a test run would rewrite the developer's model.

    :param tmp_root: Directory for the sandbox (config home, data, logs, db).
    :yields: The rig; call :meth:`ModelFlowsRig.start_host` per shape.
    """
    settings_path = Path.home() / ".claude" / "settings.json"
    settings_before: bytes | None
    try:
        settings_before = settings_path.read_bytes()
    except OSError:
        settings_before = None
    rig = ModelFlowsRig(repo=rig_repo(), root=tmp_root)
    rig.start_server()
    try:
        yield rig
    finally:
        rig.stop()
        if settings_before is not None:
            try:
                if settings_path.read_bytes() != settings_before:
                    settings_path.write_bytes(settings_before)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Pane truth
# ---------------------------------------------------------------------------


def _terminal_socket_dirs() -> set[Path]:
    """Return the omnigent terminal tmux socket dirs currently on disk."""
    tmp = Path(tempfile.gettempdir())
    return {p for p in tmp.glob("omnigent-terminal-*") if (p / "tmux.sock").exists()}


@dataclass
class PaneWatcher:
    """Discover the tmux pane a session launch creates and read its truth.

    Snapshot the terminal-socket set before creating a session; the first new
    live socket afterwards belongs to that session's terminal (the rig is the
    only thing creating sessions inside the sandbox, but other daemons on the
    machine may create panes too — the ``since`` timestamp filters those by
    directory mtime).
    """

    before: set[Path] = field(default_factory=set)
    since: float = 0.0
    socket: Path | None = None

    def arm(self) -> None:
        """Record the pre-create socket set."""
        self.before = _terminal_socket_dirs()
        self.since = time.time()

    def wait_for_pane(self, timeout: float = PANE_TIMEOUT_S) -> Path:
        """Wait for the session's tmux socket to appear.

        :param timeout: Seconds to wait.
        :returns: The tmux socket path.
        """

        def _found() -> Path | None:
            fresh = [
                d
                for d in _terminal_socket_dirs() - self.before
                if d.stat().st_mtime >= self.since - 1
            ]
            for d in sorted(fresh, key=lambda p: p.stat().st_mtime):
                sock = d / "tmux.sock"
                probe = subprocess.run(
                    ["tmux", "-S", str(sock), "list-panes", "-a"],
                    capture_output=True,
                    timeout=10,
                )
                if probe.returncode == 0 and probe.stdout.strip():
                    return sock
            return None

        self.socket = wait_for(_found, timeout=timeout, what="the session's tmux pane")
        return self.socket

    def capture(self) -> str:
        """Return the pane's current visible text."""
        assert self.socket is not None, "call wait_for_pane first"
        out = subprocess.run(
            ["tmux", "-S", str(self.socket), "capture-pane", "-p", "-t", "main"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout

    def wait_for_text(self, pattern: str, timeout: float = PANE_TIMEOUT_S) -> str:
        """Wait until the pane's visible text matches *pattern* (regex).

        :param pattern: Regex searched against the captured pane text.
        :param timeout: Seconds to wait.
        :returns: The matching captured text.
        """

        def _match() -> str | None:
            text = self.capture()
            return text if re.search(pattern, text) else None

        return wait_for(_match, timeout=timeout, what=f"pane text matching {pattern!r}")

    def type_line(self, text: str) -> None:
        """Type *text* into the pane as one bracketed paste, then Enter."""
        assert self.socket is not None, "call wait_for_pane first"
        subprocess.run(
            ["tmux", "-S", str(self.socket), "load-buffer", "-b", "omni-mf-e2e", "-"],
            input=text.encode("utf-8"),
            check=True,
            timeout=15,
        )
        subprocess.run(
            [
                "tmux",
                "-S",
                str(self.socket),
                "paste-buffer",
                "-p",
                "-b",
                "omni-mf-e2e",
                "-t",
                "main",
            ],
            check=True,
            timeout=15,
        )
        time.sleep(1.0)
        subprocess.run(
            ["tmux", "-S", str(self.socket), "send-keys", "-t", "main", "Enter"],
            check=True,
            timeout=15,
        )

    def send_key(self, key: str) -> None:
        """Send one raw tmux key (e.g. ``"Escape"``, ``"Enter"``)."""
        assert self.socket is not None, "call wait_for_pane first"
        subprocess.run(
            ["tmux", "-S", str(self.socket), "send-keys", "-t", "main", key],
            check=True,
            timeout=15,
        )


def codex_config_copy_model(session_id: str) -> str | None:
    """Return the ``model =`` line of a codex session's private config copy.

    The per-session ``CODEX_HOME`` lives under the real home dir regardless of
    ``OMNIGENT_DATA_DIR`` (it is harness state, not omnigent state) — but its
    directory is named by a runner-GENERATED bridge id, recorded only in the
    bridge's own ``state.json`` (as ``session_id``). Resolve by scanning.

    :param session_id: The session/conversation id.
    :returns: The pinned model string, or ``None``.
    """
    root = Path.home() / ".omnigent" / "codex-native"
    for state_path in root.glob("*/state.json"):
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            continue
        if state.get("session_id") != session_id:
            continue
        config = state_path.parent / "codex-home" / "config.toml"
        if not config.exists():
            return None
        for line in config.read_text().splitlines():
            match = re.match(r'^model\s*=\s*"(?P<model>[^"]+)"', line.strip())
            if match:
                return match.group("model")
        return None
    return None


# ---------------------------------------------------------------------------
# Browser driving (sync Playwright over the rig server's real SPA)
# ---------------------------------------------------------------------------


class Ui:
    """Thin user-perspective driver over the rig's SPA."""

    def __init__(self, page: Any, base_url: str) -> None:
        self.page = page
        self.base_url = base_url

    def open_landing(self) -> None:
        """Open the new-chat landing screen."""
        self.page.goto(self.base_url, wait_until="domcontentloaded")
        self.page.get_by_test_id("new-chat-landing-input").wait_for(
            state="visible", timeout=30_000
        )

    def pick_agent(self, label: str) -> None:
        """Select the agent whose dropdown label contains *label*."""
        select = self.page.get_by_test_id("new-chat-landing-agent-select")
        if label.lower() in select.inner_text().strip().lower():
            return
        select.click()
        self.page.wait_for_timeout(400)
        for item in self.page.get_by_role("menuitem").all():
            if label.lower() in item.inner_text().lower():
                item.click()
                self.page.wait_for_timeout(800)
                return
        raise AssertionError(f"agent {label!r} not offered on the landing screen")

    def open_landing_config(self) -> None:
        """Open the landing config modal (gear)."""
        self.page.get_by_test_id("new-chat-landing-config-gear").click()
        self.page.get_by_test_id("new-chat-landing-config-model").wait_for(
            state="visible", timeout=15_000
        )

    def landing_model_label(self) -> str:
        """Visible text of the landing model select trigger."""
        return self.page.get_by_test_id("new-chat-landing-config-model").inner_text().strip()

    def wait_landing_model_label(self, pattern: str, timeout_s: float = 90.0) -> str:
        """Wait until the landing model trigger matches *pattern* (regex).

        The trigger renders a bare sentinel while the host's boot probe is
        still warming (the web retries the fetch with backoff), so give the
        label the same warm-up wait a person makes.
        """

        def _match() -> str | None:
            text = self.landing_model_label()
            return text if re.search(pattern, text) else None

        return wait_for(_match, timeout=timeout_s, what=f"landing model label {pattern!r}")

    def open_model_dropdown(self, test_id: str, warmup_timeout_s: float = 90.0) -> list[str]:
        """Open a model select and return its visible option texts.

        A dropdown opened while the host's boot probe is still warming shows
        only the loading/error note and the sentinels; the web retries the
        fetch with backoff, so keep the dropdown open and re-read until model
        rows (or the settled error) appear — the same wait a person makes.

        :param test_id: Trigger test id (landing or composer variant).
        :param warmup_timeout_s: How long to allow the boot probe to warm.
        :returns: Option texts, whitespace-flattened.
        """
        self.page.get_by_test_id(test_id).click()
        deadline = time.monotonic() + warmup_timeout_s

        def _texts() -> list[str]:
            return [
                re.sub(r"\s+", " ", opt.inner_text().strip())
                for opt in self.page.get_by_role("option").all()
            ]

        while True:
            self.page.wait_for_timeout(500)
            texts = _texts()
            if any(not t.lower().startswith(("default", "smart routing")) for t in texts):
                return texts
            if time.monotonic() >= deadline:
                return texts

    def pick_dropdown_option(self, needle: str) -> None:
        """Click the open dropdown's first option containing *needle*."""
        for opt in self.page.get_by_role("option").all():
            if needle.lower() in opt.inner_text().lower():
                opt.click()
                self.page.wait_for_timeout(300)
                return
        raise AssertionError(f"dropdown option containing {needle!r} not found")

    def close_dropdown(self) -> None:
        """Dismiss an open dropdown without touching the modal."""
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(200)

    def save_landing_config(self) -> None:
        """Save the landing config modal."""
        self.page.get_by_test_id("new-chat-landing-config-save").click()
        self.page.wait_for_timeout(400)

    def submit_new_chat(self, prompt: str, timeout_s: float = 150.0) -> str:
        """Type *prompt*, submit, and wait for the session route.

        The wait must be a PLAYWRIGHT wait (`wait_for_url`), not a
        sleep-poll of ``page.url``: the sync API pumps its event dispatch
        only inside playwright calls, so a plain ``time.sleep`` loop reads a
        URL frozen at the landing route forever — the navigation this waits
        for was landing within seconds while the poll never saw it. The
        budget still covers a COLD create (host launch + harness boot).

        :returns: The new session/conversation id.
        """
        self.page.get_by_test_id("new-chat-landing-input").fill(prompt)
        self.page.get_by_test_id("new-chat-landing-submit").click()
        self.page.wait_for_url(re.compile(r"/c/[a-z0-9_-]+", re.I), timeout=timeout_s * 1000)
        match = re.search(r"/c/([a-z0-9_-]+)", self.page.url, re.I)
        assert match is not None, f"no session id in {self.page.url!r}"
        return match.group(1)

    def open_session(self, session_id: str) -> None:
        """Navigate to a session's chat page."""
        self.page.goto(f"{self.base_url}/c/{session_id}", wait_until="domcontentloaded")
        self.page.get_by_test_id("composer-config-gear").wait_for(state="visible", timeout=30_000)

    def composer_label(self) -> str:
        """Visible composer model/effort chip text ("" when absent)."""
        label = self.page.get_by_test_id("composer-model-effort-label")
        if label.count() == 0:
            return ""
        return re.sub(r"\s+", " ", label.first.inner_text().strip())

    def wait_composer_label(self, pattern: str, timeout_s: float = 60.0) -> str:
        """Wait until the composer chip matches *pattern* (regex)."""

        def _match() -> str | None:
            text = self.composer_label()
            return text if re.search(pattern, text) else None

        return wait_for(_match, timeout=timeout_s, what=f"composer label {pattern!r}")

    def open_gear(self) -> None:
        """Open the in-session config gear modal."""
        self.page.get_by_test_id("composer-config-gear").click()
        self.page.get_by_test_id("composer-config-model").wait_for(state="visible", timeout=15_000)

    def gear_model_label(self) -> str:
        """Visible text of the gear's model select trigger."""
        return self.page.get_by_test_id("composer-config-model").inner_text().strip()

    def gear_rows(self) -> list[dict[str, str]]:
        """Open the gear model dropdown and return its catalog rows.

        :returns: ``[{"id": ..., "text": ..., "active": ...}, ...]`` for every
            ``data-model-id`` row (sentinels excluded).
        """
        self.page.get_by_test_id("composer-config-model").click()
        self.page.wait_for_timeout(500)
        rows = []
        for opt in self.page.locator('[role="option"][data-model-id]').all():
            rows.append(
                {
                    "id": opt.get_attribute("data-model-id") or "",
                    "text": re.sub(r"\s+", " ", opt.inner_text().strip()),
                    "active": opt.get_attribute("data-active") or "false",
                }
            )
        return rows

    def save_gear(self) -> None:
        """Save the gear modal."""
        self.page.get_by_test_id("composer-config-save").click()
        self.page.wait_for_timeout(400)


@contextmanager
def browser_ui(base_url: str) -> Iterator[Ui]:
    """Launch a headless browser over the rig SPA.

    :param base_url: The rig server base URL.
    :yields: The :class:`Ui` driver.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_context(viewport={"width": 1440, "height": 950}).new_page()
        if os.environ.get("OMNIGENT_E2E_MODEL_FLOWS_TRACE") == "1":
            # Debug tap: print API traffic, console errors, and failed
            # requests so a stalled flow can be attributed from the test log.
            page.on(
                "response",
                lambda resp: (
                    print(f"[trace] {resp.status} {resp.request.method} {resp.url}")
                    if "/v1/" in resp.url
                    else None
                ),
            )
            page.on(
                "requestfailed",
                lambda req: print(f"[trace] REQFAIL {req.method} {req.url} :: {req.failure}"),
            )
            page.on(
                "console",
                lambda msg: (
                    print(f"[trace] CONSOLE[{msg.type}] {msg.text[:200]}")
                    if msg.type == "error"
                    else None
                ),
            )
            page.on(
                "framenavigated",
                lambda frame: (
                    print(f"[trace] NAV {frame.url}") if frame == page.main_frame else None
                ),
            )
        try:
            yield Ui(page, base_url)
        finally:
            browser.close()


def session_snapshot(base_url: str, session_id: str) -> dict[str, Any]:
    """Secondary probe: the session's REST snapshot.

    :param base_url: Rig server base URL.
    :param session_id: Session id.
    :returns: The parsed snapshot.
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=30)
    resp.raise_for_status()
    return dict(resp.json())


def host_model_options(base_url: str, host_id: str, harness: str) -> dict[str, Any]:
    """Secondary probe: the pre-launch model options for a harness.

    :returns: The parsed ``{"models": [...], ...}`` payload.
    """
    resp = httpx.get(
        f"{base_url}/v1/hosts/{host_id}/harnesses/{harness}/model-options", timeout=60
    )
    resp.raise_for_status()
    return dict(resp.json())


def dismiss_blocking_dialogs(pane: PaneWatcher, attempts: int = 3) -> None:
    """Best-effort Escape past first-run dialogs so the input box is usable.

    :param pane: The session's pane.
    :param attempts: Escape presses.
    """
    for _ in range(attempts):
        text = pane.capture()
        if "Esc to cancel" in text or "Enter to confirm" in text or "to change usage" in text:
            pane.send_key("Escape")
            time.sleep(1.0)
        else:
            return


def bypass_args(harness: str) -> list[str]:
    """First-run bypass launch args per harness (mirrors the routing suite).

    :param harness: ``"claude-native"`` or ``"codex-native"``.
    :returns: ``terminal_launch_args`` for the create call.
    """
    if harness == "claude-native":
        return ["--dangerously-skip-permissions"]
    return ["--dangerously-bypass-approvals-and-sandbox"]


def read_json(path: Path) -> dict[str, Any] | None:
    """Parse a JSON file, returning ``None`` on absence or damage."""
    try:
        return dict(json.loads(path.read_text()))
    except (OSError, ValueError):
        return None
