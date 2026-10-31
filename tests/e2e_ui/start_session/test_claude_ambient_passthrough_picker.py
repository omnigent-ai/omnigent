"""E2E: the New Chat Claude Code picker behind an ambient Anthropic passthrough gateway.

A host whose Claude Code reaches a passthrough gateway (LiteLLM: ``claude-*`` ->
``anthropic/claude-*``) only through the ambient ``ANTHROPIC_BASE_URL`` — the
standard way to point Claude Code at a proxy, with **no** anthropic
``providers:`` entry in omnigent's config — must still offer the models Claude
Code's own ``/model`` picker offers under that environment.

The rig attaches a real ``omnigent host`` (isolated HOME, the real ``claude``
CLI, fake gateway credentials, a LiteLLM-shaped listing double as the gateway)
to the suite's server and drives the real SPA's New Chat landing. A no-gateway
control runs the same host, CLI and assertions with ``ANTHROPIC_BASE_URL``
unset, so a failure in the gateway case is attributable to that variable alone.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from omnigent.harnesses.claude_native.main import (
    _claude_model_probe_invocation,
    build_native_claude_terminal_env,
)
from tests._helpers.compat import runner_executable
from tests._helpers.host import isolated_host
from tests._helpers.live_server import isolated_process_env
from tests.e2e_ui.start_session.test_native_picker_cli_parity import (
    _MODELS_SECTION_TESTID,
    _dismiss_setup_review,
    _expand_agent_config,
    _open_agent_menu,
    _pick_agent,
    _read_model_rows,
)

_HOST_ONLINE_TIMEOUT_S = 180.0
_BOOT_PROBE_TIMEOUT_S = 90.0
_PICKER_SETTLE_TIMEOUT_S = 120.0
_CLAUDE_LABEL = "Claude Code"
_MODEL_OPTIONS_URL_RE = re.compile(r"/v1/hosts/[^/]+/harnesses/[^/]+/model-options")


class _PassthroughGateway(BaseHTTPRequestHandler):
    """LiteLLM-shaped listing double: wildcard routes hidden unless asked for."""

    requests: list[dict[str, object]]

    def _record(self) -> None:
        self.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": "Authorization" in self.headers,
                "x-litellm-api-key": "x-litellm-api-key" in self.headers,
            }
        )

    def _send(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._record()
        url = urlparse(self.path)
        if url.path.rstrip("/").endswith("/models"):
            wildcard = parse_qs(url.query).get("return_wildcard_routes", [""])[0] == "true"
            data = [{"id": "claude-*", "object": "model"}] if wildcard else []
            self._send(200, {"object": "list", "data": data})
            return
        self._send(404, {"error": {"message": f"not found: {url.path}"}})

    def do_POST(self) -> None:
        self._record()
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._send(503, {"error": {"type": "api_error", "message": "listing double only"}})

    def log_message(self, *_args: object) -> None:
        return


@dataclass
class AmbientGatewayRig:
    base_url: str
    host_id: str
    host_name: str
    home: Path
    host_log: Path
    with_gateway: bool
    gateway_url: str | None
    gateway_requests: list[dict[str, object]]
    cli_rows: list[dict[str, str]]
    cli_default_model: str | None
    out_dir: Path
    notes: dict[str, object] = field(default_factory=dict)

    @property
    def expected_ids(self) -> list[str]:
        return [row["id"] for row in self.cli_rows]

    @property
    def expected_default_id(self) -> str | None:
        return next(
            (row["id"] for row in self.cli_rows if row["model"] == self.cli_default_model), None
        )

    def model_options(self) -> object:
        response = httpx.get(
            f"{self.base_url}/v1/hosts/{self.host_id}/harnesses/claude-native/model-options",
            timeout=30,
            trust_env=False,
        )
        return {"status": response.status_code, "body": response.text[:1200]}

    def persisted_catalogs(self) -> dict[str, str]:
        catalog_dir = self.home / ".omnigent" / "cache" / "model-catalogs"
        return {
            path.name: path.read_text()[:800]
            for path in sorted(catalog_dir.glob("claude-native-*"))
        }

    def wait_for_boot_probe(self) -> None:
        deadline = time.monotonic() + _BOOT_PROBE_TIMEOUT_S
        while time.monotonic() < deadline and not self.persisted_catalogs():
            time.sleep(1.0)
        self.notes["boot_probe_persisted"] = bool(self.persisted_catalogs())

    def diagnostics(self) -> str:
        def best_effort(read: Callable[[], object]) -> object:
            try:
                return read()
            except Exception as exc:
                return f"unavailable: {type(exc).__name__}: {exc}"

        return json.dumps(
            {
                "host_model_options": best_effort(self.model_options),
                "persisted_catalogs": best_effort(self.persisted_catalogs),
                "gateway_requests": self.gateway_requests,
                "cli_rows": self.cli_rows,
                "cli_default_model": self.cli_default_model,
                "notes": self.notes,
                "host_log_tail": best_effort(
                    lambda: self.host_log.read_text(errors="replace")[-1500:]
                ),
            },
            indent=1,
        )


def _cli_picker(env: dict[str, str], home: Path) -> tuple[list[dict[str, str]], str | None]:
    """Claude Code's own picker under *env*, read through omnigent's probe invocation."""
    command, args, _ = _claude_model_probe_invocation(None, stream_input=True)
    probe_env = {**env, **build_native_claude_terminal_env(None)}
    probe_env.update(DISABLE_TELEMETRY="1", DISABLE_ERROR_REPORTING="1", DISABLE_AUTOUPDATER="1")
    requests = [
        {
            "type": "control_request",
            "request_id": "model-catalog",
            "request": {"subtype": "initialize"},
        },
        {
            "type": "user",
            "message": {"role": "user", "content": "/model"},
            "parent_tool_use_id": None,
            "session_id": "default",
        },
    ]
    completed = subprocess.run(
        [command, *args],
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True,
        text=True,
        env=probe_env,
        cwd=home,
        timeout=60,
        check=False,
    )
    rows: list[dict[str, str]] = []
    default_model: str | None = None
    seen: set[str] = set()
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "control_response":
            models = (event.get("response") or {}).get("response", {}).get("models") or []
            # Like the harness, an enabled alias resolving to a disabled model is withheld.
            disabled = {
                value
                for model in models
                if model.get("disabled") is True
                for value in (model.get("value"), model.get("resolvedModel"))
                if value
            }
            for model in models:
                alias, resolved = model.get("value"), model.get("resolvedModel")
                model_id = resolved or alias
                if (
                    not model_id
                    or alias == "default"
                    or model.get("disabled") is True
                    or model_id in disabled
                    or model_id in seen
                ):
                    continue
                seen.add(model_id)
                rows.append(
                    {"id": alias, "model": model_id, "label": model.get("displayName", alias)}
                )
        elif event.get("type") == "system" and event.get("subtype") == "init":
            default_model = event.get("model")
    assert completed.returncode == 0 and rows, (
        f"Claude Code's picker enumeration failed (exit {completed.returncode}):\n"
        f"{completed.stdout[-800:]}\n{completed.stderr[-800:]}"
    )
    return rows, default_model


@pytest.fixture(scope="module", params=["ambient-passthrough-gateway", "no-gateway-control"])
def ambient_gateway_rig(
    live_server: str, tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[AmbientGatewayRig]:
    if shutil.which("claude") is None:
        pytest.skip("the 'claude' CLI is required: the picker must mirror its own /model picker")
    with_gateway = request.param == "ambient-passthrough-gateway"
    root = tmp_path_factory.mktemp(f"ambient_gateway_{request.param}")
    out_dir = Path(os.environ.get("OMNIGENT_E2E_RECORD_DIR") or root) / request.param
    out_dir.mkdir(parents=True, exist_ok=True)

    gateway_requests: list[dict[str, object]] = []
    with _passthrough_gateway(gateway_requests) as gateway_url:
        yield from _ambient_gateway_rig(
            request, live_server, root, out_dir, with_gateway, gateway_url, gateway_requests
        )


@contextlib.contextmanager
def _passthrough_gateway(requests: list[dict[str, object]]) -> Iterator[str]:
    """Serve the LiteLLM-shaped listing double on a loopback port for the block's duration."""
    handler = type("Gateway", (_PassthroughGateway,), {"requests": requests})
    gateway = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{gateway.server_address[1]}"
    finally:
        gateway.shutdown()
        gateway.server_close()


def _ambient_gateway_rig(
    request: pytest.FixtureRequest,
    live_server: str,
    root: Path,
    out_dir: Path,
    with_gateway: bool,
    gateway_url: str,
    gateway_requests: list[dict[str, object]],
) -> Iterator[AmbientGatewayRig]:
    """Enumerate the real CLI, attach a real host to the server and yield the rig."""
    # The reported shell: a gateway URL plus Claude Code credentials, nothing in
    # omnigent's config. The control drops only ANTHROPIC_BASE_URL.
    ambient = {
        "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-fake-subscription-token",
        "ANTHROPIC_CUSTOM_HEADERS": "x-litellm-api-key: fake-litellm-virtual-key",
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }
    if with_gateway:
        ambient["ANTHROPIC_BASE_URL"] = gateway_url
    base_env = {
        "PATH": os.environ["PATH"],
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    home = root / "home"
    home.mkdir()
    cli_rows, cli_default = _cli_picker(isolated_process_env(home, ambient, base_env), home)
    assert cli_default in {row["model"] for row in cli_rows}, (
        f"the CLI's default {cli_default!r} is not among its picker rows {cli_rows}"
    )
    gateway_requests.clear()  # attribute later traffic to the host alone

    host_name = f"gateway-{request.param[:8]}-{root.name[-6:]}"
    command = [
        runner_executable(),
        "-m",
        "omnigent",
        "host",
        "--server",
        live_server,
        "--non-interactive",
        "--no-open",
    ]
    with (
        httpx.Client(base_url=live_server, trust_env=False) as client,
        isolated_host(
            root, live_server, env=ambient, base_env=base_env, name=host_name, command=command
        ) as host,
    ):
        row = host.wait_online(client, timeout=_HOST_ONLINE_TIMEOUT_S, poll_interval=1)
        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        while (row.get("configured_harnesses") or {}).get("claude-native") is not True:
            assert time.monotonic() < deadline, (
                f"claude-native never became ready: {row.get('configured_harnesses')}"
            )
            time.sleep(2.0)
            row = next(
                (
                    r
                    for r in client.get("/v1/hosts").json()["hosts"]
                    if r["host_id"] == host.host_id
                ),
                None,
            )
            assert row is not None, f"host {host.host_id} vanished from /v1/hosts"
        rig = AmbientGatewayRig(
            base_url=live_server,
            host_id=host.host_id,
            host_name=host_name,
            home=home,
            host_log=host.log_path,
            with_gateway=with_gateway,
            gateway_url=gateway_url if with_gateway else None,
            gateway_requests=gateway_requests,
            cli_rows=cli_rows,
            cli_default_model=cli_default,
            out_dir=out_dir,
        )
        rig.wait_for_boot_probe()
        yield rig
        (out_dir / "rig.json").write_text(rig.diagnostics())


def _select_host(page: Page, rig: AmbientGatewayRig) -> None:
    """Pick the rig's host in the landing host menu and confirm it is the active one."""
    chip = page.get_by_test_id("new-chat-landing-host-chip")
    menu = page.locator('[data-slot="dropdown-menu-content"]')
    option = page.get_by_test_id(f"new-chat-landing-host-{rig.host_id}")
    expect(chip).to_be_visible(timeout=30_000)
    chip.click()
    expect(option).to_be_visible(timeout=30_000)
    expect(option).to_have_attribute("title", f"{rig.host_name} — online")
    option.click()
    expect(menu).to_have_count(0)
    chip.click()
    expect(option).to_have_attribute("data-active", "true")
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0)


def _settled_models_section(page: Page) -> tuple[str, list[dict[str, str]]]:
    """Open Claude Code's config flyout and wait for its Models section to settle.

    Mirrors a person re-opening the flyout while the host's boot probe warms:
    settled means the section no longer says "Loading models…" and shows either
    model rows or its empty-state text.
    """
    deadline = time.monotonic() + _PICKER_SETTLE_TIMEOUT_S
    section_text = ""
    while time.monotonic() < deadline:
        _open_agent_menu(page)
        if page.get_by_test_id(_MODELS_SECTION_TESTID).count() == 0:
            # Menus re-render while queries settle; a miss here just retries.
            with contextlib.suppress(AssertionError, PlaywrightError):
                _expand_agent_config(page, _CLAUDE_LABEL)
        page.wait_for_timeout(500)
        section = page.get_by_test_id(_MODELS_SECTION_TESTID)
        if section.count() == 0:
            continue
        section_text = " ".join((section.first.inner_text() or "").split())
        rows = _read_model_rows(section.first)
        if rows or ("Loading models" not in section_text and section_text):
            return section_text, rows
    raise AssertionError(
        f"the Claude Code Models section never settled within {_PICKER_SETTLE_TIMEOUT_S:.0f}s "
        f"(last text: {section_text!r})"
    )


@pytest.mark.timeout(600)
def test_claude_picker_offers_the_clis_models_behind_an_ambient_passthrough_gateway(
    request: pytest.FixtureRequest, live_server: str, ambient_gateway_rig: AmbientGatewayRig
) -> None:
    """The landing Claude Code picker lists what the CLI's /model picker lists."""
    rig = ambient_gateway_rig
    page: Page = request.getfixturevalue("page")
    model_option_requests: list[str] = []
    page.on(
        "request",
        lambda req: (
            model_option_requests.append(req.url)
            if _MODEL_OPTIONS_URL_RE.search(req.url)
            else None
        ),
    )
    page.goto(live_server)
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
    _dismiss_setup_review(page)
    _select_host(page, rig)
    _pick_agent(page, _CLAUDE_LABEL)

    section_text, rows = _settled_models_section(page)
    page.wait_for_timeout(2_500)
    page.screenshot(path=str(rig.out_dir / "models-section.png"))
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    picker_text = " ".join((picker.inner_text() or "").split())
    rig.notes.update(
        section_text=section_text,
        rows=rows,
        picker_text=picker_text,
        model_option_requests=model_option_requests,
    )
    if page.video is not None:
        (rig.out_dir / "video-path.txt").write_text(page.video.path())

    this_host = f"/v1/hosts/{rig.host_id}/harnesses/claude-native/model-options"
    assert any(this_host in url for url in model_option_requests), (
        f"the landing never asked this host for Claude Code's models: {model_option_requests}"
    )
    row_ids = [row["id"] for row in rows]
    assert row_ids == rig.expected_ids, (
        f"the Claude Code picker offered {row_ids} (Models section read {section_text!r}, picker "
        f"button read {picker_text!r}) but the CLI's own /model picker offers {rig.expected_ids} "
        f"under this host's environment (ANTHROPIC_BASE_URL={rig.gateway_url!r}, no providers: "
        f"entry)\n{rig.diagnostics()}"
    )
    checked = [row["id"] for row in rows if row["checked"] == "true"]
    assert checked == [rig.expected_default_id], (
        f"the picker marks {checked} as default; the CLI's default resolves to "
        f"{rig.cli_default_model!r} ({rig.expected_default_id!r})"
    )
    expect(picker).not_to_contain_text("Models unavailable")

    listings = [req for req in rig.gateway_requests if str(req["path"]).startswith("/v1/models")]
    if rig.with_gateway:
        # The host asked the gateway the way Claude Code talks to it: wildcard
        # routes requested, bearer token and custom header attached.
        assert listings, f"the host never asked the gateway what it serves\n{rig.diagnostics()}"
        assert all(
            "return_wildcard_routes=true" in str(req["path"])
            and req["authorization"] is True
            and req["x-litellm-api-key"] is True
            for req in listings
        ), rig.diagnostics()
    else:
        assert rig.gateway_requests == []
