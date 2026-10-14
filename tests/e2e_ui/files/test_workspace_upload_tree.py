"""Tree destinations, spring-load and bounded byte-exact transfers through the real stack."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

import psutil
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.files.test_workspace_upload import (
    _drag,
    _open_uploads,
    _sha256,
)
from tests.e2e_ui.files.test_workspace_upload import upload_server_pids as upload_server_pids
from tests.e2e_ui.files.test_workspace_upload import upload_session as upload_session


def _assert_label_inside_rail(page: Page) -> None:
    label = page.get_by_text("Upload to src/assets", exact=True).bounding_box()
    rail = page.get_by_role("complementary", name="Workspace").bounding_box()
    assert label is not None and rail is not None
    assert rail["x"] <= label["x"]
    assert label["x"] + label["width"] <= rail["x"] + rail["width"] + 1
    assert rail["y"] <= label["y"]
    assert label["y"] + label["height"] <= rail["y"] + rail["height"] + 1


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_workspace_upload_tree_narrow_label(
    page: Page, upload_session: tuple[str, str, Path], theme: str
) -> None:
    """The floating label stays visible in the smallest rail in either theme."""
    _, sid, workspace = upload_session
    nested = workspace / "src" / "assets"
    nested.mkdir(parents=True)
    (nested / "existing.txt").write_text("original")
    state = json.dumps(
        [{"id": sid, "state": {"open": True, "widthPx": 240, "rightRailTab": "files"}}]
    )
    page.add_init_script(
        f"localStorage.setItem('web-theme', {json.dumps(theme)});"
        f"localStorage.setItem('omnigent:session-workspace-state', {json.dumps(state)});"
    )
    page.emulate_media(reduced_motion="reduce")
    _open_uploads(page, upload_session)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="src/", exact=True).click()
    assets = rail.get_by_role("button", name="assets/", exact=True)
    _drag(page, '[data-drop-dir="src/assets"][data-drop-folder]', "dragover")
    expect(page.get_by_text("Upload to src/assets", exact=True)).to_be_visible()
    _assert_label_inside_rail(page)
    bounds = rail.bounding_box()
    assert bounds is not None and bounds["width"] <= 242
    assert page.locator("html").evaluate("e => e.classList.contains('dark')") == (theme == "dark")
    assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
    expect(assets).to_have_attribute("aria-expanded", "true", timeout=5000)


def test_workspace_upload_tree_folder_file_and_spring_load(
    page: Page, upload_session: tuple[str, str, Path]
) -> None:
    """Nested drops refresh an expanded folder without losing expansion or scroll."""
    _, _, workspace = upload_session
    nested = workspace / "src" / "assets"
    nested.mkdir(parents=True)
    (nested / "existing.txt").write_text("original")
    for index in range(80):
        (nested / f"row_{index:03d}.txt").write_text("row")
    _open_uploads(page, upload_session)
    rail = page.get_by_role("complementary", name="Workspace")
    src = rail.get_by_role("button", name="src/", exact=True)
    src.click()
    assets = rail.get_by_role("button", name="assets/", exact=True)
    expect(assets).to_have_attribute("aria-expanded", "false")
    _drag(page, '[data-drop-dir="src/assets"][data-drop-folder]', "dragover")
    expect(page.get_by_text("Upload to src/assets", exact=True)).to_be_visible()
    _assert_label_inside_rail(page)
    expect(page.locator('[data-upload-hover][data-drop-dir="src/assets"]')).to_have_count(1)
    expect(page.locator('[data-drop-variant="upload"]')).to_have_count(1)
    expect(assets).to_have_attribute("aria-expanded", "true", timeout=5000)
    _drag(page, '[data-drop-dir="src/assets"][data-drop-folder]', "drop", name="folder-drop.txt")
    tray = rail.get_by_role("region", name="Uploads")
    expect(tray.get_by_text("Done", exact=True)).to_have_count(1, timeout=30_000)
    expect(
        rail.get_by_role("button", name=re.compile(r"^folder-drop\.txt(?:\s|$)"))
    ).to_be_visible(timeout=30_000)
    assert (nested / "folder-drop.txt").read_bytes() == b"rail bytes"
    expect(src).to_have_attribute("aria-expanded", "true")
    expect(assets).to_have_attribute("aria-expanded", "true")
    _drag(
        page,
        '[data-drop-dir="src/assets"]:not([data-drop-folder])',
        "drop",
        name="file-parent.txt",
    )
    expect(tray.get_by_text("Done", exact=True)).to_have_count(2, timeout=30_000)
    assert (nested / "file-parent.txt").read_bytes() == b"rail bytes"
    assert not (workspace / "file-parent.txt").exists()
    expect(page.get_by_role("button", name="Remove file-parent.txt")).to_have_count(0)
    section = rail.locator("section[data-upload-tree]")
    section.evaluate("element => { element.scrollTop = 300; }")
    expect(section).to_have_js_property("scrollTop", 300)
    _drag(page, '[data-drop-dir="src/assets"]:not([data-drop-folder])', "dragover")
    expect(page.get_by_text("Upload to src/assets", exact=True)).to_be_visible()
    _drag(
        page,
        '[data-drop-dir="src/assets"]:not([data-drop-folder])',
        "drop",
        name="row_015_new.txt",
    )
    expect(tray.get_by_text("Done", exact=True)).to_have_count(3, timeout=30_000)
    expect(
        rail.get_by_role("button", name=re.compile(r"^row_015_new\.txt(?:\s|$)"))
    ).to_be_visible(timeout=30_000)
    expect(section).to_have_js_property("scrollTop", 300)
    expect(assets).to_have_attribute("aria-expanded", "true")
    assert (nested / "row_015_new.txt").read_bytes() == b"rail bytes"
    # Re-rooted folders expose paths relative to that root, then resolve against it.
    section.evaluate("element => { element.scrollTop = 0; }")
    src.dblclick()
    expect(rail.get_by_role("button", name="assets/", exact=True)).to_be_visible()
    _drag(page, '[data-drop-dir="assets"][data-drop-folder]', "drop", name="rerooted.txt")
    expect(tray.get_by_text("Done", exact=True)).to_have_count(4, timeout=30_000)
    assert (nested / "rerooted.txt").read_bytes() == b"rail bytes"


@pytest.fixture
def large_tree_transfer(
    page: Page,
    upload_session: tuple[str, str, Path],
    upload_server_pids: dict[str, int],
    tmp_path: Path,
) -> tuple[dict[str, Any], list[tuple[Path, Path]], Path]:
    """Drive one real multi-file transfer for independent byte and memory assertions."""
    base, sid, workspace = upload_session
    nested = workspace / "src" / "assets"
    nested.mkdir(parents=True)
    (nested / "existing.txt").write_text("original")
    _open_uploads(page, upload_session)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="src/", exact=True).click()
    assets = rail.get_by_role("button", name="assets/", exact=True)
    assets.click()
    expect(rail.get_by_role("button", name="existing.txt", exact=True)).to_be_visible()
    source = tmp_path / "source"
    source.mkdir()
    binary = source / "large.bin"
    with binary.open("wb") as stream:
        for _ in range(200):
            stream.write(os.urandom(1024**2))
    texts = [source / "one.txt", source / "two.txt"]
    for path in texts:
        path.write_text(f"upload tree: {path.name}\n")
    # Native file selection supplies browser File handles without a base64 payload.
    page.evaluate("""() => {
      const input = document.createElement('input');
      input.type = 'file'; input.multiple = true; input.id = 'tree-proof-files';
      document.body.appendChild(input);
    }""")
    page.locator("#tree-proof-files").set_input_files([binary, *texts])
    process = psutil.Process(upload_server_pids[base])
    baseline = process.memory_info().rss
    samples = [baseline]
    stop = threading.Event()

    def sample() -> None:
        while not stop.wait(0.01):
            samples.append(process.memory_info().rss)

    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    try:
        row = page.locator('[data-drop-dir="src/assets"][data-drop-folder]')
        for event in ["dragover", "drop"]:
            row.evaluate(
                """(element, event) => {
              const transfer = new DataTransfer();
              for (const file of document.querySelector('#tree-proof-files').files) {
                transfer.items.add(file);
              }
              element.dispatchEvent(new DragEvent(event, {
                bubbles:true, cancelable:true, dataTransfer:transfer
              }));
            }""",
                event,
            )
            if event == "dragover":
                expect(page.get_by_text("Upload to src/assets", exact=True)).to_be_visible()
        tray = rail.get_by_role("region", name="Uploads")
        expect(tray.get_by_role("progressbar", name="large.bin upload progress")).to_be_visible()
        expect(tray.get_by_text("Done", exact=True)).to_have_count(3, timeout=180_000)
        expect(rail.get_by_role("button", name=re.compile(r"^large\.bin(?:\s|$)"))).to_be_visible(
            timeout=30_000
        )
        expect(assets).to_have_attribute("aria-expanded", "true")
    finally:
        stop.set()
        monitor.join(timeout=5)
        page.locator("#tree-proof-files").evaluate("element => element.remove()")
    files = []
    for path in [binary, *texts]:
        local, disk = _sha256(path), _sha256(nested / path.name)
        files.append(
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "local_sha256": local,
                "disk_sha256": disk,
            }
        )
    capability = page.request.get(
        f"{base}/v1/sessions/{sid}/resources/environments/default/uploads"
    ).json()
    chunk, in_flight = capability["limits"]["chunk"]["max"], capability["limits"]["parallel_files"]
    headroom = 32 * 1024**2
    limit = baseline + headroom + 2 * chunk * in_flight
    evidence = {
        "lane": "local caller_process, sandbox none",
        "files": files,
        "server_baseline_rss": baseline,
        "server_peak_rss": max(samples),
        "chunk_bytes": chunk,
        "in_flight": in_flight,
        "baseline_headroom": headroom,
        "rss_limit": limit,
    }
    output = os.environ.get("WORKSPACE_UPLOAD_TREE_EVIDENCE")
    if output:
        Path(output).write_text(json.dumps(evidence, indent=2))
    return evidence, [(path, nested / path.name) for path in [binary, *texts]], workspace


def test_workspace_upload_tree_large_binary_byte_exact(
    large_tree_transfer: tuple[dict[str, Any], list[tuple[Path, Path]], Path],
) -> None:
    """The 200MiB binary and siblings arrive unchanged only in the nested destination."""
    _, files, workspace = large_tree_transfer
    for source, destination in files:
        assert destination.parent == workspace / "src" / "assets"
        assert destination.stat().st_size == source.stat().st_size
        assert _sha256(destination) == _sha256(source)
        assert not (workspace / source.name).exists()


def test_workspace_upload_tree_isolated_rss(
    large_tree_transfer: tuple[dict[str, Any], list[tuple[Path, Path]], Path],
) -> None:
    """Keep the transfer-layer memory gate independent of byte and destination coverage."""
    evidence, _, _ = large_tree_transfer
    assert evidence["server_peak_rss"] < evidence["rss_limit"], evidence
