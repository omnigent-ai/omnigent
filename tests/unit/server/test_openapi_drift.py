"""Strict drift detector for the checked-in OpenAPI artifact.

Whereas :mod:`tests.unit.server.test_stream_events` validates a specific
slice of the spec (SSE route surfaces), this test does a byte-for-byte
comparison between :file:`openapi.json` (the artifact committed at the
repo root) and the spec produced live by :func:`generate_spec` in
:mod:`scripts.dump_openapi`. Any divergence — added/removed paths,
schema changes, or even whitespace differences in the serialized JSON
— fails this test with a unified diff and explicit regen instructions.

The contract:

* ``openapi.json`` is the source of truth that external SDK / docs
  tooling reads. It must always match what ``scripts/dump_openapi.py``
  emits against the current FastAPI app.
* When a developer changes routes or response schemas, this test
  fails until they run the dump script and commit the result.

This test does NOT exercise behavior — it exclusively guards the
on-disk artifact.
"""

from __future__ import annotations

import difflib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

# Locate ``scripts/dump_openapi.py`` and import it dynamically.
# ``scripts/`` is not a package on the Python path, so a normal
# ``from scripts.dump_openapi import ...`` would not resolve. We
# load it via ``importlib`` so the test is robust to the layout.
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent.parent
_DUMP_OPENAPI_PATH: Path = _REPO_ROOT / "scripts" / "dump_openapi.py"
_OPENAPI_JSON_PATH: Path = _REPO_ROOT / "openapi.json"

# Cap on diff-snippet size in the failure message. Larger diffs
# truncate at this line count so the assertion message stays
# readable; the developer regenerates and inspects the full diff
# via ``git diff openapi.json`` after running the script.
_MAX_DIFF_LINES: int = 60


def _load_dump_openapi_module() -> Any:
    """
    Import :mod:`scripts.dump_openapi` from its file path.

    The ``scripts/`` directory is not on ``sys.path`` by default, so a
    regular import would fail. ``importlib.util`` lets us load the
    module directly from its known location.

    :returns: The imported :mod:`scripts.dump_openapi` module object,
        exposing :func:`generate_spec`.
    """
    spec = importlib.util.spec_from_file_location(
        "scripts_dump_openapi",
        _DUMP_OPENAPI_PATH,
    )
    assert spec is not None and spec.loader is not None, (
        f"Could not locate dump_openapi.py at {_DUMP_OPENAPI_PATH}. "
        f"The script must exist at the documented path."
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["scripts_dump_openapi"] = module
    spec.loader.exec_module(module)
    return module


def _summarize_path_changes(
    on_disk: dict[str, Any],
    generated: dict[str, Any],
) -> str:
    """
    Build a one-line summary of path-level changes.

    Compares the top-level ``paths`` keys of the two specs. The result
    is informational only — it does NOT replace the unified diff,
    which captures field-level changes inside each operation.

    :param on_disk: The parsed ``openapi.json`` contents.
    :param generated: The freshly generated spec dict.
    :returns: A summary string, e.g.
        ``"+2 paths added, -1 removed, ~3 changed"``.
    """
    on_disk_paths = set(on_disk.get("paths", {}).keys())
    generated_paths = set(generated.get("paths", {}).keys())
    added = generated_paths - on_disk_paths
    removed = on_disk_paths - generated_paths
    # "changed" = same path key, different operation dict body.
    overlap = on_disk_paths & generated_paths
    changed = {p for p in overlap if on_disk["paths"][p] != generated["paths"][p]}
    return f"+{len(added)} paths added, -{len(removed)} removed, ~{len(changed)} changed"


def _build_failure_message(
    on_disk_text: str,
    generated_text: str,
    on_disk_spec: dict[str, Any],
    generated_spec: dict[str, Any],
) -> str:
    """
    Compose the assertion message for a drift failure.

    Includes (in order):

    1. A path-level change summary.
    2. A capped unified diff snippet so the developer can see what
       diverged at a glance.
    3. Explicit regen instructions as the LAST lines, which is the
       single most important payload — the test exists to point at
       this command.

    :param on_disk_text: Serialized contents of ``openapi.json`` on disk.
    :param generated_text: Serialized contents emitted by
        :func:`generate_spec`.
    :param on_disk_spec: Parsed dict of the on-disk file.
    :param generated_spec: Parsed dict of the generated spec.
    :returns: A multi-line string suitable as the second arg to
        ``assert ... , msg``.
    """
    summary = _summarize_path_changes(on_disk_spec, generated_spec)
    diff_lines = list(
        difflib.unified_diff(
            on_disk_text.splitlines(keepends=True),
            generated_text.splitlines(keepends=True),
            fromfile="openapi.json (on disk)",
            tofile="openapi.json (generated)",
            n=2,
        ),
    )
    truncated = ""
    if len(diff_lines) > _MAX_DIFF_LINES:
        diff_lines = diff_lines[:_MAX_DIFF_LINES]
        truncated = (
            f"\n... (diff truncated at {_MAX_DIFF_LINES} lines — run "
            f"`git diff openapi.json` after regenerating to see full diff)"
        )
    diff_text = "".join(diff_lines) + truncated

    # NOTE: the regen instructions are the LAST lines of this
    # message by design — pytest's assertion-rewrite formatting
    # truncates long messages from the top, so putting the call to
    # action at the bottom maximizes the chance the developer sees it.
    return (
        "openapi.json on disk is out of sync with the generator output.\n"
        f"Summary: {summary}\n"
        "\n"
        "Diff (truncated):\n"
        f"{diff_text}\n"
        "\n"
        "To regenerate the artifact:\n"
        f"  cd {_REPO_ROOT}\n"
        "  .venv/bin/python scripts/dump_openapi.py\n"
        "  git add openapi.json\n"
        "\n"
        "If the new spec is intentional, commit the regenerated file.\n"
        "If accidental, revert the route/schema change that caused it."
    )


def test_openapi_json_matches_generator_output() -> None:
    """
    ``openapi.json`` on disk matches :func:`generate_spec` byte-for-byte.

    Production breakage that causes this test to fail: a developer
    added a route, changed a request/response schema, added a tag,
    or renamed an operation without rerunning
    ``scripts/dump_openapi.py`` to refresh the checked-in artifact.
    External SDK / docs tooling consumes ``openapi.json`` directly,
    so the artifact must always reflect the live app.

    The serialization mirrors ``scripts/dump_openapi.py`` exactly
    (``json.dumps(..., indent=2, sort_keys=True) + "\\n"``); any
    formatting change there must be matched here to keep the
    comparison meaningful.

    Sibling to
    :func:`test_openapi_json_surfaces_sse_routes_with_typed_schema` in
    :mod:`tests.unit.server.test_stream_events`, which validates only the
    SSE-route slice — this test is strictly a drift detector.
    """
    assert _OPENAPI_JSON_PATH.exists(), (
        f"openapi.json not found at {_OPENAPI_JSON_PATH}. "
        f"Run `.venv/bin/python scripts/dump_openapi.py` to generate it, "
        f"then commit the resulting file."
    )

    dump_module = _load_dump_openapi_module()
    generated_spec = dump_module.generate_spec()
    # Match dump_openapi.py's serialization exactly so the comparison
    # is byte-for-byte against what the script would write.
    generated_text = json.dumps(generated_spec, indent=2, sort_keys=True) + "\n"

    on_disk_text = _OPENAPI_JSON_PATH.read_text()

    if on_disk_text == generated_text:
        return

    # Drift detected — assemble a useful failure message before raising.
    on_disk_spec = json.loads(on_disk_text)
    raise AssertionError(
        _build_failure_message(
            on_disk_text=on_disk_text,
            generated_text=generated_text,
            on_disk_spec=on_disk_spec,
            generated_spec=generated_spec,
        ),
    )
