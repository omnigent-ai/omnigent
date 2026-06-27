"""E2E: harness label map renders correctly in the SPA.

Verifies that ``BRAIN_HARNESS_LABELS`` from ``ap-web/src/lib/agentLabels.ts``
is bundled into the SPA and used to render human-friendly harness names.
The test inspects the built JavaScript bundle for the expected label entries
rather than requiring a live agent session per harness, since most harnesses
(e.g. ``rovo-cli``) need a CLI binary that CI runners do not have.

This is an SPA build-time correctness check: if a new harness label is added
to the source map but tree-shaken away or misspelled, this test catches it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUILD_DIR = _REPO_ROOT / "omnigent" / "server" / "static" / "web-ui"


@pytest.mark.parametrize(
    "harness_key,expected_label",
    [
        ("rovo-cli", "Rovo Dev"),
        ("claude-sdk", "Claude SDK"),
        ("openai-agents", "OpenAI Agents SDK"),
        ("codex", "Codex"),
        ("pi", "Pi"),
    ],
)
def test_harness_label_in_built_spa(
    built_spa: None,
    harness_key: str,
    expected_label: str,
) -> None:
    """Each BRAIN_HARNESS_LABELS entry is present in the built SPA bundle.

    Scans the compiled JS assets for the harness key and its display label.
    This ensures the label map survives the Vite build (no tree-shaking,
    no typo in the source) without needing a live harness session.

    :param built_spa: Session-scoped fixture that builds the SPA.
    :param harness_key: The internal harness identifier, e.g. ``"rovo-cli"``.
    :param expected_label: The user-facing label, e.g. ``"Rovo Dev"``.
    """
    js_files = list(_BUILD_DIR.rglob("*.js"))
    assert js_files, f"No JS files found in {_BUILD_DIR}; SPA build may have failed."

    bundle_text = "\n".join(f.read_text(errors="replace") for f in js_files)
    assert harness_key in bundle_text, (
        f"Harness key {harness_key!r} not found in any built JS bundle. "
        f"Check BRAIN_HARNESS_LABELS in ap-web/src/lib/agentLabels.ts."
    )
    assert expected_label in bundle_text, (
        f"Label {expected_label!r} for harness {harness_key!r} not found in "
        f"any built JS bundle. The label may be misspelled or tree-shaken."
    )
