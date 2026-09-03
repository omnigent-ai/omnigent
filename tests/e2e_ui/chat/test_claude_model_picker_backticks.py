"""E2E: claude-native picker shows plain model names, never literal backticks.

Claude Code 2.1.250 started printing its ``Current model:`` label as markdown
code (`` `Sonnet 5` ``). The catalog probe reads that label verbatim, so the
"Configure Claude Code" model picker renders every row inside literal
backticks — and the two 1M-context rows read differently from each other,
because the ``(1M context)`` marker is appended outside the backticks for
``sonnet[1m]`` (the CLI omits the marker there) while ``opus[1m]``'s
CLI-provided label already carries it inside the backticks.

The journey: start a claude-native session whose catalog was probed from
Claude Code 2.1.250 → open the composer's Configure Claude Code gear → open
the Model picker → every row must read as a plain model name and both
1M-context rows must read the same way.

The catalog is produced by the REAL probe pipeline
(``omnigent.claude_native.claude_model_catalog``) against a stub ``claude``
CLI whose stream-json output is byte-identical to what a real Claude Code
2.1.250 ``claude -p "/model"`` run printed when captured live — so the test
is deterministic regardless of which CLI version this machine has installed,
while every line of Omnigent's parsing/composition code still runs for real.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from omnigent.claude_native import claude_model_catalog
from tests.e2e_ui.conftest import fetch_with_retry

# What each picker row must read as once the harness's markdown-code label
# formatting is neutralized: plain names, with both 1M rows phrased alike.
_EXPECTED_LABELS = [
    ("sonnet", "Sonnet 5"),
    ("opus", "Opus 5"),
    ("haiku", "Haiku 4.5"),
    ("fable", "Fable 5"),
    ("sonnet[1m]", "Sonnet 5 (1M context)"),
    ("opus[1m]", "Opus 5 (1M context)"),
]

# Every visible 1M-context row must follow this one shape — a bare model name
# followed by the marker, no wrapping punctuation anywhere.
_ONE_M_ROW_SHAPE = re.compile(r"[\w.]+(?: [\w.]+)* \(1M context\)")

# The stub CLI replays Claude Code 2.1.250's stream-json ``/model`` answers,
# captured verbatim from the real 2.1.250 binary: the enumeration run (no
# --model) prints the Usage alias list plus a backticked default label, and
# each --model <alias> resolution run prints its backticked label with the
# alias's exact model id in the init event.
_FAKE_CLAUDE_CLI = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import sys

    RESOLUTIONS = {
        "sonnet": ("claude-sonnet-5", "`Sonnet 5`"),
        "opus": ("claude-opus-5", "`Opus 5`"),
        "haiku": ("claude-haiku-4-5-20251001", "`Haiku 4.5`"),
        "fable": ("claude-fable-5", "`Fable 5`"),
        "best": ("claude-opus-5", "`Opus 5`"),
        "sonnet[1m]": ("claude-sonnet-5[1m]", "`Sonnet 5`"),
        "opus[1m]": ("claude-opus-5[1m]", "`Opus 5 (1M context)`"),
        "fable[1m]": ("claude-fable-5", "`Fable 5`"),
        "opusplan": ("claude-sonnet-5", "`Opus in plan mode, else Sonnet`"),
        "default": ("claude-opus-5[1m]", "`Opus 5 (1M context) (default)`"),
    }
    USAGE = (
        "Usage: /model <name>. Available: sonnet, opus, haiku, fable, best, "
        "sonnet[1m], opus[1m], fable[1m], opusplan, default, or a full model ID."
    )

    args = sys.argv[1:]
    alias = args[args.index("--model") + 1] if "--model" in args else None
    if alias is None:
        model, label = RESOLUTIONS["default"]
        text = f"Current model: {label}\\n{USAGE}"
    else:
        model, label = RESOLUTIONS.get(alias, RESOLUTIONS["default"])
        text = f"Current model: {label}"
    print(json.dumps({"type": "system", "subtype": "init", "model": model}))
    print(json.dumps({"type": "result", "result": text}))
    """
)


def _probe_catalog_from_claude_2_1_250(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[dict[str, object]]:
    """Run the real catalog probe against the stubbed 2.1.250 ``claude``.

    :param monkeypatch: Used to front-load the stub's bin dir onto ``PATH``.
    :param tmp_path: Per-test dir the stub executable is written into.
    :returns: The wire-ready picker rows the probe composed.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text(_FAKE_CLAUDE_CLI)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    # The e2e_ui suite already runs under an event loop, so drive the async
    # probe on its own loop in a worker thread.
    with ThreadPoolExecutor(max_workers=1) as executor:
        catalog = executor.submit(asyncio.run, claude_model_catalog(None)).result(timeout=120)
    assert catalog is not None, "the stubbed claude CLI probe must not fail"
    return catalog


def _patch_session_as_claude_native(
    page: Page,
    session_id: str,
    model_options: list[dict[str, object]],
    llm_model: str,
) -> None:
    """Reshape the browser's session snapshot into a claude-native session.

    The server fixture seeds a normal ``hello_world`` session so the page can
    boot against the real app/server; this route patch changes only the
    ``GET /v1/sessions/{session_id}`` response the browser sees, exposing the
    probed catalog rows as the session's model options.

    :param page: Playwright page, before navigation.
    :param session_id: The seeded session's id.
    :param model_options: Catalog rows the picker should render.
    :param llm_model: Bound model id reported for the session.
    """

    def _handle(route: Route) -> None:
        request = route.request
        if urlparse(request.url).path != f"/v1/sessions/{session_id}" or request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["labels"] = {
            **payload.get("labels", {}),
            "omnigent.wrapper": "claude-code-native-ui",
        }
        payload["harness"] = "claude"
        payload["llm_model"] = llm_model
        payload["model_options"] = model_options
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


def _open_model_picker(page: Page, base_url: str, session_id: str) -> None:
    """Navigate to the session and open the Configure Claude Code model list.

    :param page: Playwright page with the session route already patched.
    :param base_url: The live server's base URL.
    :param session_id: The seeded session's id.
    """
    page.goto(f"{base_url}/c/{session_id}")
    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    gear.click()
    page.get_by_test_id("composer-config-model").click()


def test_claude_native_picker_shows_plain_model_names(
    page: Page,
    seeded_session: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every picker row reads as a plain model name — no literal backticks.

    Claude Code 2.1.250 prints its labels as markdown code; nothing may pass
    those backticks through to the picker rows or to the "Default (…)"
    sentinel row.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session; the browser snapshot is patched claude-native.
    :param monkeypatch: For the stub CLI's ``PATH`` front-load.
    :param tmp_path: Where the stub CLI is written.
    """
    base_url, session_id = seeded_session
    catalog = _probe_catalog_from_claude_2_1_250(monkeypatch, tmp_path)
    _patch_session_as_claude_native(page, session_id, catalog, llm_model="claude-opus-5[1m]")

    _open_model_picker(page, base_url, session_id)

    rows = page.locator('[role="option"][data-model-id]')
    expect(rows).to_have_count(len(_EXPECTED_LABELS))
    for index, (model_id, label) in enumerate(_EXPECTED_LABELS):
        row = rows.nth(index)
        expect(row).to_have_attribute("data-model-id", model_id)
        expect(row).to_contain_text(label)
        expect(row).not_to_contain_text("`")

    # The "Default (…)" sentinel row names the default via the same label, so
    # the backticks must not leak into it either.
    default_row = page.get_by_role("option", name=re.compile(r"^Default \("))
    expect(default_row).not_to_contain_text("`")


def test_claude_native_picker_1m_context_rows_read_alike(
    page: Page,
    seeded_session: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Both 1M-context rows follow one shape: ``<Name> (1M context)``.

    The CLI's backticked labels put the 1M marker outside the backticks on
    one row (``sonnet[1m]``) and inside them on the other (``opus[1m]``);
    the two rows must instead read identically-shaped plain labels.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session; the browser snapshot is patched claude-native.
    :param monkeypatch: For the stub CLI's ``PATH`` front-load.
    :param tmp_path: Where the stub CLI is written.
    """
    base_url, session_id = seeded_session
    catalog = _probe_catalog_from_claude_2_1_250(monkeypatch, tmp_path)
    _patch_session_as_claude_native(page, session_id, catalog, llm_model="claude-opus-5[1m]")

    _open_model_picker(page, base_url, session_id)

    rows = page.locator('[role="option"][data-model-id]')
    expect(rows).to_have_count(len(_EXPECTED_LABELS))
    one_m_texts = [
        rows.nth(index).inner_text().strip()
        for index in range(len(_EXPECTED_LABELS))
        if "1M context" in rows.nth(index).inner_text()
    ]
    assert len(one_m_texts) == 2, f"expected two 1M-context rows, saw {one_m_texts!r}"
    for text in one_m_texts:
        assert _ONE_M_ROW_SHAPE.fullmatch(text), (
            f"1M-context row {text!r} does not read as a plain '<Name> (1M context)' label"
        )
