"""Workspace uploads through the real server, local runner and browser."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Page, Route, expect

from tests._helpers.browser import user_page
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import (
    _TEST_AGENT_YAML,
    fetch_with_retry,
    open_right_rail,
)


@pytest.fixture
def upload_session(
    built_spa: None, mock_llm_server_url: str, tmp_path: Path
) -> Iterator[tuple[str, str, Path]]:
    """Uploads need a primary-session runner; the suite's shared runner has none."""
    workspace = tmp_path / "upload-workspace"
    workspace.mkdir()
    spec = _TEST_AGENT_YAML.replace("cwd: .", f"cwd: {workspace}")
    bundle = bundle_files({"hello_world.yaml": spec.encode()})
    repo = Path(__file__).resolve().parents[3]
    mock_env = {"OPENAI_BASE_URL": f"{mock_llm_server_url}/v1", "OPENAI_API_KEY": "mock-key"}
    with server_runner(
        tmp_path / "stack",
        workspace=workspace,
        server_cwd=repo,
        server_env={"OMNIGENT_FEATURES": "workspace_upload", "PYTHONPATH": str(repo), **mock_env},
    ) as stack:
        response = post_session_bundle(
            httpx.post, f"{stack.base_url}/v1/sessions", bundle, timeout=30
        )
        response.raise_for_status()
        sid = response.json()["session_id"]
        stack.start_runner(
            env={"OMNIGENT_RUNNER_PRIMARY_SESSION_ID": sid, "PYTHONPATH": str(repo), **mock_env},
            cwd=repo,
        )
        bind_session_runner(httpx.patch, stack.base_url, sid, stack.runner_id, timeout=30)
        try:
            yield stack.base_url, sid, workspace
        finally:
            httpx.delete(f"{stack.base_url}/v1/sessions/{sid}", timeout=30)


def _open_uploads(page: Page, session: tuple[str, str, Path]) -> None:
    base, sid, _ = session
    page.goto(f"{base}/c/{sid}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    capability = page.request.get(
        f"{base}/v1/sessions/{sid}/resources/environments/default/uploads"
    )
    assert capability.status == 200, capability.text()
    assert capability.json()["supported"], capability.text()
    expect(rail.get_by_role("button", name="Upload files")).to_be_enabled(timeout=30_000)


def _drag(page: Page, selector: str, event: str, *, name: str = "rail.txt") -> bool:
    return page.locator(selector).first.evaluate(
        """(element, {event, name}) => {
          const transfer = new DataTransfer();
          transfer.items.add(new File(['rail bytes'], name, {type: 'text/plain'}));
          const drag = new DragEvent(event, {
            bubbles: true, cancelable: true, dataTransfer: transfer
          });
          element.dispatchEvent(drag);
          return drag.defaultPrevented;
        }""",
        {"event": event, "name": name},
    )


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def test_workspace_upload_picker_binary_and_drop_are_byte_exact(
    page: Page, upload_session: tuple[str, str, Path], tmp_path: Path
) -> None:
    """Multi-file picker and rail drop commit atomic, byte-exact files and refresh the tree."""
    _open_uploads(page, upload_session)
    _, _, workspace = upload_session
    source = tmp_path / "source"
    source.mkdir()
    size = int(os.environ.get("WORKSPACE_UPLOAD_PROOF_BYTES", str(20 * 1024**2)))
    binary = source / "random.bin"
    binary.write_bytes(os.urandom(size))
    texts = [source / "one.txt", source / "two.txt"]
    for path in texts:
        path.write_text(f"workspace upload: {path.name}\n")
    rail = page.get_by_role("complementary", name="Workspace")
    with page.expect_file_chooser() as chooser:
        rail.get_by_role("button", name="Upload files").click()
    chooser.value.set_files([binary, *texts])
    tray = rail.get_by_role("region", name="Uploads")
    expect(tray).to_be_visible()
    expect(tray.get_by_role("progressbar", name="random.bin upload progress")).to_be_visible()
    expect(tray.get_by_text("Done", exact=True)).to_have_count(3, timeout=120_000)
    for path in [binary, *texts]:
        assert (workspace / path.name).is_file()
        assert _sha256(path) == _sha256(workspace / path.name)
        expect(
            rail.get_by_role("button", name=re.compile(rf"^{re.escape(path.name)}(?:\s|$)"))
        ).to_be_visible(timeout=30_000)
    _drag(page, "[data-upload-drop-root]", "dragover")
    expect(page.locator('[data-drop-variant="upload"]')).to_be_visible()
    expect(page.get_by_text("to upload-workspace", exact=True)).to_be_visible()
    expect(page.locator('[data-drop-variant="attach"]')).to_have_count(0)
    _drag(page, "[data-upload-drop-root]", "drop")
    expect(tray.get_by_text("Done", exact=True)).to_have_count(4, timeout=30_000)
    assert (workspace / "rail.txt").read_text() == "rail bytes"
    expect(page.get_by_role("button", name="Remove rail.txt")).to_have_count(0)
    evidence = os.environ.get("WORKSPACE_UPLOAD_EVIDENCE")
    if evidence:
        files = [
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "local_sha256": _sha256(path),
                "disk_sha256": _sha256(workspace / path.name),
                "disk_path": str(workspace / path.name),
            }
            for path in [binary, *texts]
        ]
        files.append(
            {
                "name": "rail.txt",
                "bytes": 10,
                "local_sha256": hashlib.sha256(b"rail bytes").hexdigest(),
                "disk_sha256": _sha256(workspace / "rail.txt"),
            }
        )
        Path(evidence).write_text(
            json.dumps({"runner": "local caller_process, sandbox none", "files": files}, indent=2)
        )


@pytest.mark.parametrize("choice", ["Replace", "Keep both", "Skip"])
def test_workspace_upload_conflict_dialog(
    page: Page, upload_session: tuple[str, str, Path], choice: str
) -> None:
    """One batch dialog implements all three no-clobber decisions."""
    _, _, workspace = upload_session
    for name in ["same.txt", "also.txt"]:
        (workspace / name).write_text("original")
    _open_uploads(page, upload_session)
    page.get_by_label("Choose workspace files").set_input_files(
        [
            {"name": name, "mimeType": "text/plain", "buffer": b"replacement"}
            for name in ["same.txt", "also.txt"]
        ]
    )
    dialog = page.get_by_role("dialog", name="Files already exist")
    expect(dialog).to_be_visible()
    expect(page.get_by_role("dialog")).to_have_count(1)
    expect(dialog.get_by_text("same.txt", exact=True)).to_be_visible()
    expect(dialog.get_by_text("also.txt", exact=True)).to_be_visible()
    dialog.get_by_role("button", name=choice, exact=True).click()
    expect(dialog).to_have_count(0, timeout=30_000)
    tray = page.get_by_role("complementary", name="Workspace").get_by_role(
        "region", name="Uploads"
    )
    expect(
        tray.get_by_text("Cancelled" if choice == "Skip" else "Done", exact=True)
    ).to_have_count(2, timeout=30_000)
    for name in ["same.txt", "also.txt"]:
        assert (workspace / name).read_text() == (
            "replacement" if choice == "Replace" else "original"
        )
        renamed = workspace / name.replace(".txt", " (1).txt")
        assert renamed.exists() == (choice == "Keep both")
        if choice == "Keep both":
            assert renamed.read_text() == "replacement"


def test_workspace_upload_cancel_and_overlay_swap(
    page: Page, upload_session: tuple[str, str, Path]
) -> None:
    """Cancel removes staging; rail/chat swap stays exclusive; missed drops never navigate."""
    _open_uploads(page, upload_session)
    _, _, workspace = upload_session

    def delayed_chunk(route: Route) -> None:
        if route.request.method == "PATCH":
            response = fetch_with_retry(route)
            time.sleep(0.25)
            route.fulfill(response=response)
        else:
            route.continue_()

    page.route("**/uploads/*", delayed_chunk)
    page.get_by_label("Choose workspace files").set_input_files(
        {
            "name": "cancel.bin",
            "mimeType": "application/octet-stream",
            "buffer": os.urandom(20 * 1024**2),
        }
    )
    rail = page.get_by_role("complementary", name="Workspace")
    progress = rail.get_by_role("progressbar", name="cancel.bin upload progress")
    expect(progress).to_be_visible()
    expect(rail.locator('[data-upload-state="uploading"]')).to_be_visible()
    with page.expect_response(
        lambda response: response.request.method == "DELETE" and "/uploads/" in response.url
    ) as deleted:
        rail.get_by_role("button", name="Cancel cancel.bin", exact=True).click()
    assert deleted.value.status in (204, 410)
    expect(rail.get_by_text("Cancelled", exact=True)).to_be_visible()
    expect(rail.get_by_role("button", name=re.compile(r"^cancel\.bin(?:\s|$)"))).to_have_count(0)
    page.wait_for_timeout(500)
    assert not (workspace / "cancel.bin").exists()
    assert not list(workspace.glob("*.omnigent-upload-*.part"))
    page.unroute_all(behavior="ignoreErrors")
    _drag(page, "[data-upload-drop-root]", "dragover")
    expect(page.locator('[data-drop-variant="upload"]')).to_have_count(1)
    assert page.locator("[data-upload-drop-root]").first.evaluate(
        "element => element.closest('[data-chat-surface]') === null"
    )
    _drag(page, "[data-chat-surface]", "dragover", name="chat.txt")
    expect(page.locator('[data-drop-variant="upload"]')).to_have_count(0)
    expect(page.locator('[data-drop-variant="attach"]')).to_have_count(1)
    expect(page.get_by_text("Attach to message", exact=True)).to_be_visible()
    expect(page.get_by_text("Sent with your next message", exact=True)).to_be_visible()
    _drag(page, "[data-chat-surface]", "drop", name="chat.txt")
    expect(page.get_by_role("button", name="Remove chat.txt")).to_be_visible()
    assert not (workspace / "chat.txt").exists()
    page.get_by_role("button", name="Remove chat.txt").click()
    url = page.url
    assert _drag(page, "body", "drop", name="near-miss.txt")
    expect(page.locator('[data-testid="file-drop-overlay"]')).to_have_count(0)
    assert page.url == url
    assert not (workspace / "near-miss.txt").exists()


def test_workspace_upload_viewer_has_no_affordance(
    browser: Browser, upload_session: tuple[str, str, Path]
) -> None:
    """A genuine view-only identity cannot see upload controls or intercept drops."""
    base, sid, _ = upload_session
    email = "upload-viewer@ui.test"
    httpx.put(
        f"{base}/v1/sessions/{sid}/permissions", json={"user_id": email, "level": 1}, timeout=30
    ).raise_for_status()
    httpx.patch(
        f"{base}/v1/sessions/{sid}", json={"share_workspace_files": True}, timeout=30
    ).raise_for_status()
    with user_page(browser, headers={"X-Forwarded-Email": email}) as page:
        page.goto(f"{base}/c/{sid}")
        expect(page.locator("[data-chat-surface]")).to_be_visible(timeout=30_000)
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        rail.get_by_role("tab", name=re.compile("^Files")).click()
        expect(rail.get_by_role("tab", name=re.compile("^Files"))).to_have_attribute(
            "aria-selected", "true"
        )
        expect(page.get_by_role("button", name="Upload files")).to_have_count(0)
        expect(page.locator("[data-upload-drop-root]")).to_have_count(0)
        expect(page.get_by_role("region", name="Uploads")).to_have_count(0)


def test_workspace_upload_mobile_drawer(page: Page, upload_session: tuple[str, str, Path]) -> None:
    """The drawer exposes the same picker and durable tray on touch layouts."""
    page.set_viewport_size({"width": 390, "height": 844})
    base, sid, workspace = upload_session
    page.goto(f"{base}/c/{sid}")
    page.get_by_role("button", name="Conversation actions").click()
    page.get_by_role("menuitem", name="Files").click()
    drawer = page.get_by_test_id("files-panel-drawer")
    expect(drawer).to_be_visible()
    expect(drawer.get_by_role("button", name="Upload files")).to_be_enabled(timeout=30_000)
    drawer.get_by_label("Choose workspace files").set_input_files(
        {"name": "mobile.txt", "mimeType": "text/plain", "buffer": b"mobile"}
    )
    expect(
        drawer.get_by_role("region", name="Uploads").get_by_text("Done", exact=True)
    ).to_be_visible(timeout=30_000)
    assert (workspace / "mobile.txt").read_bytes() == b"mobile"
