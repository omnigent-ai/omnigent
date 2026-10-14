"""Tree destinations, spring-load and bounded byte-exact transfers through the real stack."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
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


_MIB = 1024**2
_ALLOCATOR_SLACK = 64 * _MIB
# Steady-state small-to-large peak deltas measured at most 18 MiB; retaining every
# chunk adds at least 168 MiB. Fixed, so it never scales with file size.
_PLATEAU_MARGIN = 32 * _MIB
_SETTLE_TIMEOUT_S = 30.0


def _open_tree_destination(page: Page, upload_session: tuple[str, str, Path]) -> Path:
    """Open the rail with ``src/assets`` expanded as the drop destination."""
    nested = upload_session[2] / "src" / "assets"
    nested.mkdir(parents=True)
    (nested / "existing.txt").write_text("original")
    _open_uploads(page, upload_session)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="src/", exact=True).click()
    rail.get_by_role("button", name="assets/", exact=True).click()
    expect(rail.get_by_role("button", name="existing.txt", exact=True)).to_be_visible()
    return nested


def _random_file(path: Path, size: int) -> Path:
    with path.open("wb") as stream:
        for _ in range(size // _MIB):
            stream.write(os.urandom(_MIB))
    return path


def _drop_tree_files(page: Page, paths: list[Path], done: int, *, progress: str = "") -> None:
    """Drop real browser File handles on ``src/assets`` and wait for ``done`` rows."""
    rail = page.get_by_role("complementary", name="Workspace")
    # Native file selection supplies browser File handles without a base64 payload.
    page.evaluate("""() => {
      const input = document.createElement('input');
      input.type = 'file'; input.multiple = true; input.id = 'tree-proof-files';
      document.body.appendChild(input);
    }""")
    page.locator("#tree-proof-files").set_input_files(paths)
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
        if progress:
            expect(tray.get_by_role("progressbar", name=progress)).to_be_visible()
        expect(tray.get_by_text("Done", exact=True)).to_have_count(done, timeout=180_000)
        for path in paths:
            expect(
                rail.get_by_role("button", name=re.compile(rf"^{re.escape(path.name)}(?:\s|$)"))
            ).to_be_visible(timeout=30_000)
        expect(rail.get_by_role("button", name="assets/", exact=True)).to_have_attribute(
            "aria-expanded", "true"
        )
    finally:
        page.locator("#tree-proof-files").evaluate("element => element.remove()")


def _peak_rss(process: psutil.Process, action: Callable[[], None]) -> int:
    """Run ``action`` while sampling ``process`` RSS every 10 ms; return the peak."""
    samples = [process.memory_info().rss]
    stop = threading.Event()

    def sample() -> None:
        while not stop.wait(0.01):
            samples.append(process.memory_info().rss)

    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    try:
        action()
    finally:
        stop.set()
        monitor.join(timeout=5)
    return max(samples)


def _upload_limits(page: Page, upload_session: tuple[str, str, Path]) -> tuple[int, int]:
    base, sid, _ = upload_session
    capability = page.request.get(
        f"{base}/v1/sessions/{sid}/resources/environments/default/uploads"
    ).json()
    return capability["limits"]["chunk"]["max"], capability["limits"]["parallel_files"]


def _tree_sources(tmp_path: Path) -> list[Path]:
    source = tmp_path / "source"
    source.mkdir()
    texts = [source / "one.txt", source / "two.txt"]
    for path in texts:
        path.write_text(f"upload tree: {path.name}\n")
    return [_random_file(source / "large.bin", 200 * _MIB), *texts]


@pytest.fixture
def large_tree_transfer(
    page: Page, upload_session: tuple[str, str, Path], tmp_path: Path
) -> tuple[dict[str, Any], list[tuple[Path, Path]], Path]:
    """Drive one real multi-file transfer for byte and destination assertions."""
    nested = _open_tree_destination(page, upload_session)
    paths = _tree_sources(tmp_path)
    _drop_tree_files(page, paths, 3, progress="large.bin upload progress")
    files = [
        {
            "name": path.name,
            "bytes": path.stat().st_size,
            "local_sha256": _sha256(path),
            "disk_sha256": _sha256(nested / path.name),
        }
        for path in paths
    ]
    evidence = {"lane": "local caller_process, sandbox none", "files": files}
    output = os.environ.get("WORKSPACE_UPLOAD_TREE_EVIDENCE")
    if output:
        Path(output).write_text(json.dumps(evidence, indent=2))
    return evidence, [(path, nested / path.name) for path in paths], upload_session[2]


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
    page: Page,
    upload_session: tuple[str, str, Path],
    upload_server_pids: dict[str, int],
    tmp_path: Path,
) -> None:
    """Server RSS plateaus at a small upload's peak, under the in-flight bound, then settles."""
    _open_tree_destination(page, upload_session)
    chunk, in_flight = _upload_limits(page, upload_session)
    small = _random_file(tmp_path / "small.bin", 4 * chunk)
    paths = _tree_sources(tmp_path)
    process = psutil.Process(upload_server_pids[upload_session[0]])
    baseline = process.memory_info().rss
    small_peak = _peak_rss(process, lambda: _drop_tree_files(page, [small], 1))
    large_peak = _peak_rss(
        process,
        lambda: _drop_tree_files(page, paths, 4, progress="large.bin upload progress"),
    )
    settled, started = process.memory_info().rss, time.monotonic()
    while settled > baseline + _ALLOCATOR_SLACK and time.monotonic() - started < _SETTLE_TIMEOUT_S:
        time.sleep(0.25)
        settled = process.memory_info().rss
    evidence = {
        "lane": "local caller_process, sandbox none",
        "chunk_bytes": chunk,
        "in_flight": in_flight,
        "small_bytes": small.stat().st_size,
        "large_bytes": paths[0].stat().st_size,
        "server_baseline_rss": baseline,
        "server_small_peak_rss": small_peak,
        "server_large_peak_rss": large_peak,
        "server_settled_rss": settled,
        "settle_wait_s": round(time.monotonic() - started, 2),
        "plateau_margin": _PLATEAU_MARGIN,
        "rss_ceiling": baseline + 3 * chunk * in_flight + _ALLOCATOR_SLACK,
        "settle_ceiling": baseline + _ALLOCATOR_SLACK,
    }
    output = os.environ.get("WORKSPACE_UPLOAD_TREE_RSS_EVIDENCE")
    if output:
        Path(output).write_text(json.dumps(evidence, indent=2))
    assert large_peak <= evidence["rss_ceiling"], evidence
    assert large_peak - small_peak <= _PLATEAU_MARGIN, evidence
    assert settled <= evidence["settle_ceiling"], evidence
