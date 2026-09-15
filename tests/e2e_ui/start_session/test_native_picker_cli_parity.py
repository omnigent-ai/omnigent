"""E2E: New Chat native model pickers follow the CLI's own catalog.

Omnigent's pre-launch model pickers must offer what the corresponding CLI's own
``/model`` picker offers under the same launch configuration:

- **Codex**: a source ``config.toml`` carrying ``model_catalog_json`` (which
  REPLACES codex's bundled catalog) and a default ``model`` must surface in
  the picker — not codex's built-in catalog and built-in default.
- **Claude Code**: the picker must not offer aliases the CLI's interactive
  ``/model`` picker withholds (``fable``), even though the ``Available:``
  usage line advertises every settable alias and ``--model fable`` resolves.

The rig boots a real ``omnigent server`` + ``omnigent host`` with an isolated
``$HOME``. The Codex side drives the real ``codex`` CLI (its ``model/list`` is
credential-free). The Claude side drives a scripted CLI double whose
interactive picker disables ``fable`` (offering Sonnet/Opus/Haiku) while its
help output advertises ``fable`` — the exact mismatch the report reproduces.
The browser drives the real SPA's New Chat landing screen.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]

_SERVER_HEALTH_TIMEOUT_S = 90.0
_HOST_READY_TIMEOUT_S = 120.0
_PICKER_WARMUP_TIMEOUT_S = 120.0

_CODEX_CUSTOM_VISIBLE = ("acme-large", "acme-small", "acme-turbo", "acme-nano")
_CODEX_HIDDEN_SLUG = "acme-secret"
_CODEX_CONFIGURED_DEFAULT = "acme-large"
_CODEX_CONFIGURED_DEFAULT_LABEL = "Acme Large"

_CLAUDE_PICKER_ALIASES = ("sonnet", "opus", "haiku")
_CLAUDE_EXTRA_ALIAS = "fable"
_CLAUDE_EXTRA_LABEL = "Fable 5"

# A scripted Claude Code double modelling the reported account shape: the
# interactive /model picker (the control-protocol initialize options) disables
# fable, while the headless "/model" usage line advertises fable too and
# `--model fable` resolves.
_CLAUDE_STUB = '''#!/usr/bin/env python3
"""Scripted Claude Code double: interactive picker disables fable; help advertises it."""

import json
import sys

ARGS = sys.argv[1:]

RESOLUTIONS = {
    "sonnet": ("claude-sonnet-4-5-20250929", "Sonnet 4.5"),
    "opus": ("claude-opus-4-1-20250805", "Opus 4.1"),
    "haiku": ("claude-haiku-4-5-20251001", "Haiku 4.5"),
    "fable": ("claude-fable-5-20251115", "Fable 5"),
    "default": ("claude-sonnet-4-5-20250929", "Sonnet 4.5"),
}
HELP_ALIASES = "sonnet, opus, haiku, fable, default, or a full model ID"

# What the CLI's own interactive /model picker offers: fable is present but
# disabled for this account.
PICKER_MODELS = [
    {"value": "default", "resolvedModel": RESOLUTIONS["default"][0],
     "displayName": "Default (recommended)"},
    {"value": "sonnet", "resolvedModel": RESOLUTIONS["sonnet"][0], "displayName": "Sonnet 4.5"},
    {"value": "opus", "resolvedModel": RESOLUTIONS["opus"][0], "displayName": "Opus 4.1"},
    {"value": "haiku", "resolvedModel": RESOLUTIONS["haiku"][0], "displayName": "Haiku 4.5"},
    {"value": "fable", "resolvedModel": RESOLUTIONS["fable"][0], "displayName": "Fable 5",
     "disabled": True, "description": "Requires usage credits"},
]


def opt(flag):
    if flag in ARGS:
        index = ARGS.index(flag)
        if index + 1 < len(ARGS):
            return ARGS[index + 1]
    return None


def emit(payload):
    print(json.dumps(payload))


def emit_current_model(alias):
    model, label = RESOLUTIONS.get(alias or "default", (alias or "?", alias or "?"))
    emit({
        "type": "system",
        "subtype": "init",
        "session_id": "stub-session",
        "model": model,
        "tools": [],
    })
    if alias:
        text = "Current model: `" + label + "`"
    else:
        text = (
            "Current model: `" + label + "` (default)\\n\\n"
            "Usage: /model <name>. Available: " + HELP_ALIASES + "."
        )
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text})


if "--version" in ARGS:
    print("2.1.236 (Claude Code)")
    raise SystemExit(0)

if ARGS[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claudeai"}))
    raise SystemExit(0)

if "-p" in ARGS and opt("--input-format") == "stream-json":
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        if event.get("type") == "control_request":
            emit({
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": event.get("request_id"),
                    "response": {"commands": [], "models": PICKER_MODELS},
                },
            })
    emit_current_model(opt("--model"))
    raise SystemExit(0)

if "-p" in ARGS:
    prompt = opt("-p") or ""
    alias = opt("--model")
    if prompt.strip().startswith("/model") or alias:
        emit_current_model(alias)
    else:
        model, _ = RESOLUTIONS.get(alias or "default", (alias or "?", alias or "?"))
        emit({
            "type": "system",
            "subtype": "init",
            "session_id": "stub-session",
            "model": model,
            "tools": [],
        })
        emit({"type": "result", "subtype": "success", "is_error": False, "result": "ok"})
    raise SystemExit(0)

print("stub claude: unsupported invocation: " + " ".join(ARGS), file=sys.stderr)
raise SystemExit(1)
'''


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(predicate: Callable[[], object], timeout_s: float, what: str) -> object:
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
        except Exception as exc:  # retried until the deadline
            last_exc = exc
            result = None
        if result:
            return result
        time.sleep(1.0)
    detail = f" (last error: {last_exc})" if last_exc is not None else ""
    raise AssertionError(f"timed out after {timeout_s:.0f}s waiting for {what}{detail}")


def _subprocess_pythonpath() -> str:
    return os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
        ]
    )


def _sanitized_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("OMNIGENT_RUNNER", "OMNIGENT_PROCESS", "ANTHROPIC_", "OPENAI_")):
            env.pop(key)
    for key in ("CLAUDECODE", "RUNNER_SERVER_URL", "OMNIGENT", "CODEX_HOME"):
        env.pop(key, None)
    env["PYTHONPATH"] = _subprocess_pythonpath()
    return env


def _build_codex_source_home(root: Path, home: Path) -> list[str]:
    """Write the user's ``~/.codex`` shape from the report: catalog + default.

    :returns: The visible slugs the CLI's own ``/model`` picker offers.
    """
    codex = shutil.which("codex")
    assert codex is not None
    bundled_home = root / "codex-bundled-probe"
    bundled_home.mkdir(parents=True, exist_ok=True)
    env = _sanitized_env()
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(bundled_home)
    bundled = json.loads(
        subprocess.run(
            [codex, "debug", "models"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            check=True,
        ).stdout
    )
    models = bundled["models"]
    template = next(m for m in models if m.get("visibility") == "list")
    visible = [m for m in models if m.get("visibility") == "list"]
    hidden = [m for m in models if m.get("visibility") != "list"]

    def _clone(slug: str, visibility: str) -> dict:
        entry = copy.deepcopy(template)
        entry.update(
            {
                "slug": slug,
                "display_name": slug.replace("-", " ").title(),
                "visibility": visibility,
                "availability_nux": None,
                "upgrade": None,
            }
        )
        return entry

    custom = [_clone(slug, "list") for slug in _CODEX_CUSTOM_VISIBLE]
    catalog = {
        **bundled,
        "models": [*visible, *custom, *hidden, _clone(_CODEX_HIDDEN_SLUG, "hide")],
    }
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    catalog_path = codex_home / "model-catalog.json"
    catalog_path.write_text(json.dumps(catalog))
    # The custom provider table keeps codex-native "ready" without a codex
    # login; the minimal probe copy preserves provider tables, so it cannot
    # mask the dropped catalog/default this test reproduces.
    (codex_home / "config.toml").write_text(
        f'model = "{_CODEX_CONFIGURED_DEFAULT}"\n'
        f'model_catalog_json = "{catalog_path}"\n'
        'model_provider = "acme"\n'
        "\n"
        "[model_providers.acme]\n"
        'name = "Acme"\n'
        'base_url = "http://127.0.0.1:1/v1"\n'
        'wire_api = "responses"\n'
    )
    return [m["slug"] for m in catalog["models"] if m.get("visibility") == "list"]


@dataclass
class PickerRig:
    """A booted server + host whose CLIs carry the reported configurations."""

    base_url: str
    host_id: str
    codex_visible_slugs: list[str]
    server_log: Path
    host_log: Path

    def log_tail(self) -> str:
        parts = []
        for path in (self.server_log, self.host_log):
            if path.exists():
                parts.append(f"--- {path.name} ---\n{path.read_text(errors='replace')[-3000:]}")
        return "\n".join(parts)


@pytest.fixture(scope="module")
def picker_rig(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PickerRig]:
    if shutil.which("codex") is None:
        pytest.skip("the 'codex' CLI is required to probe the real catalog behaviour")

    root = tmp_path_factory.mktemp("picker_parity_rig")
    home = root / "home"
    home.mkdir()
    stub_bin = root / "stub-bin"
    stub_bin.mkdir()
    stub = stub_bin / "claude"
    stub.write_text(_CLAUDE_STUB)
    stub.chmod(0o755)
    codex_visible = _build_codex_source_home(root, home)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_log = root / "server.log"
    host_log = root / "host.log"
    logs = []

    server_env = _sanitized_env()
    server_env["OMNIGENT_CONFIG_HOME"] = str(root / "server-config-home")
    server_env["OMNIGENT_DATA_DIR"] = str(root / "server-data")
    server_handle = open(server_log, "w")  # noqa: SIM115 — subprocess lifetime
    logs.append(server_handle)
    server = subprocess.Popen(
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
            f"sqlite:///{root / 'rig.db'}",
            "--artifact-location",
            str(root / "artifacts"),
        ],
        env=server_env,
        cwd=str(_REPO_ROOT),
        stdout=server_handle,
        stderr=subprocess.STDOUT,
    )

    host_env = _sanitized_env()
    host_env["HOME"] = str(home)
    host_env["PATH"] = f"{stub_bin}{os.pathsep}{os.environ['PATH']}"
    host_env["OMNIGENT_CONFIG_HOME"] = str(root / "host-config-home")
    host_env["OMNIGENT_DATA_DIR"] = str(root / "host-data")
    host_handle = open(host_log, "w")  # noqa: SIM115 — subprocess lifetime
    logs.append(host_handle)
    host: subprocess.Popen[bytes] | None = None

    def _stop(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    try:

        def _healthy() -> bool:
            if server.poll() is not None:
                raise AssertionError(
                    f"rig server exited early:\n{server_log.read_text(errors='replace')[-3000:]}"
                )
            try:
                return httpx.get(f"{base_url}/health", timeout=2).status_code == 200
            except httpx.HTTPError:
                return False

        _wait_for(_healthy, _SERVER_HEALTH_TIMEOUT_S, "the rig server /health")

        host = subprocess.Popen(
            [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", base_url],
            env=host_env,
            cwd=str(_REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=host_handle,
        )

        def _host_ready() -> str | None:
            if host is not None and host.poll() is not None:
                raise AssertionError(
                    f"rig host exited early:\n{host_log.read_text(errors='replace')[-3000:]}"
                )
            rows = httpx.get(f"{base_url}/v1/hosts", timeout=5).json().get("hosts", [])
            for row in rows:
                if row.get("status") != "online":
                    continue
                readiness = row.get("configured_harnesses") or {}
                claude_ready = readiness.get("claude-native") is True
                if claude_ready and readiness.get("codex-native") is True:
                    return str(row["host_id"])
            return None

        host_id = _wait_for(
            _host_ready, _HOST_READY_TIMEOUT_S, "the rig host to register with both CLIs ready"
        )

        yield PickerRig(
            base_url=base_url,
            host_id=str(host_id),
            codex_visible_slugs=codex_visible,
            server_log=server_log,
            host_log=host_log,
        )
    finally:
        _stop(host)
        _stop(server)
        for handle in logs:
            handle.close()


_MODEL_ROW_PREFIX = "new-chat-landing-agent-model-"
# Non-catalog controls sharing the row testid prefix: the smart-routing
# toggle, the "Harness default" sentinel, the search box, and the trigger's
# current-model label (`…-agent-model-value`).
_MODEL_ROW_SENTINELS = ("smart-routing", "default", "search", "value")


def _open_agent_menu(page: Page) -> None:
    select = page.get_by_test_id("new-chat-landing-agent-select")
    expect(select).to_be_visible(timeout=30_000)
    if page.get_by_role("menu").count() == 0:
        select.click()
        expect(page.get_by_role("menu").first).to_be_visible(timeout=10_000)


def _pick_agent(page: Page, label: str) -> None:
    _open_agent_menu(page)
    for item in page.get_by_role("menuitem").all():
        if label.lower() in item.inner_text().lower():
            item.click()
            page.wait_for_timeout(800)
            return
    raise AssertionError(f"agent {label!r} not offered on the landing screen")


def _model_rows(page: Page, rig: PickerRig) -> list[dict[str, str]]:
    """Read the selected agent's model rows from the landing agent dropdown.

    The host's boot probe may still be warming; the SPA retries the fetch
    with backoff, so keep re-reading (reopening the menu when a click closed
    it) until catalog rows appear — the same wait a person makes.
    """
    deadline = time.monotonic() + _PICKER_WARMUP_TIMEOUT_S
    while time.monotonic() < deadline:
        _open_agent_menu(page)
        page.wait_for_timeout(500)
        rows: list[dict[str, str]] = []
        for option in page.locator(
            f'[role="menuitemcheckbox"][data-testid^="{_MODEL_ROW_PREFIX}"]'
        ).all():
            testid = option.get_attribute("data-testid") or ""
            row_id = testid[len(_MODEL_ROW_PREFIX) :]
            if row_id in _MODEL_ROW_SENTINELS:
                continue
            rows.append(
                {
                    "id": row_id,
                    "text": " ".join((option.inner_text() or "").split()),
                    "checked": option.get_attribute("aria-checked") or "",
                }
            )
        if rows:
            return rows
    raise AssertionError(
        f"the landing agent menu showed no model rows within {_PICKER_WARMUP_TIMEOUT_S:.0f}s\n"
        f"{rig.log_tail()}"
    )


def test_codex_picker_offers_the_clis_configured_catalog_and_default(
    page: Page, picker_rig: PickerRig
) -> None:
    """The New Chat Codex picker mirrors ``model_catalog_json`` + ``model``.

    The CLI's own ``/model`` picker under this ``config.toml`` offers exactly
    the configured catalog's visible entries with ``acme-large`` as the
    effective default (verified live: ``model/list`` against the same home).
    The Omnigent picker must offer the same choices and name the same default.
    """
    page.goto(picker_rig.base_url)
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
    _pick_agent(page, "Codex")

    rows = _model_rows(page, picker_rig)
    row_ids = [row["id"] for row in rows]

    missing = [slug for slug in picker_rig.codex_visible_slugs if slug not in row_ids]
    assert not missing, (
        f"the Codex picker dropped the CLI's configured catalog entries {missing}: it offered "
        f"{row_ids} — codex's built-in catalog instead of the source config.toml's "
        f"model_catalog_json (the CLI's own /model picker offers "
        f"{picker_rig.codex_visible_slugs})"
    )
    unexpected = [row_id for row_id in row_ids if row_id not in picker_rig.codex_visible_slugs]
    assert not unexpected, (
        f"the Codex picker offered {unexpected}, which the CLI's /model picker does not "
        f"(configured visible catalog: {picker_rig.codex_visible_slugs})"
    )
    hidden = [row_id for row_id in row_ids if row_id == _CODEX_HIDDEN_SLUG]
    assert not hidden, f"hidden catalog entries must stay absent from the picker: {hidden}"

    checked = [row["id"] for row in rows if row["checked"] == "true"]
    assert checked == [_CODEX_CONFIGURED_DEFAULT], (
        f"the picker marks {checked} as the default choice; the CLI's effective default under "
        f"this config.toml is model = {_CODEX_CONFIGURED_DEFAULT!r} "
        f"({_CODEX_CONFIGURED_DEFAULT_LABEL})"
    )


def test_claude_picker_omits_aliases_the_cli_picker_does_not_offer(
    page: Page, picker_rig: PickerRig
) -> None:
    """The New Chat Claude picker must not offer ``fable``.

    The CLI's interactive ``/model`` picker offers Sonnet/Opus/Haiku only;
    its headless usage line ("Available: …") also advertises ``fable``, and
    ``--model fable`` resolves. The picker must follow the interactive
    picker's choices, not the help alias enumeration.
    """
    page.goto(picker_rig.base_url)
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
    _pick_agent(page, "Claude Code")

    rows = _model_rows(page, picker_rig)
    row_ids = [row["id"] for row in rows]

    missing = [alias for alias in _CLAUDE_PICKER_ALIASES if alias not in row_ids]
    assert not missing, (
        f"the Claude picker dropped choices the CLI's /model picker offers: {missing} "
        f"(offered rows: {row_ids})"
    )
    fable_rows = [
        row
        for row in rows
        if row["id"] == _CLAUDE_EXTRA_ALIAS or _CLAUDE_EXTRA_LABEL in row["text"]
    ]
    assert not fable_rows, (
        f"the Claude picker offers {fable_rows}, but the CLI's interactive /model picker does "
        f"not offer fable — it is advertised only by the headless usage line, which must not "
        f"drive the picker"
    )
