"""Host-side model discovery for builtin ACP CLI harnesses.

A builtin ACP CLI row (see :mod:`omnigent.acp_cli_harnesses`) can declare a
``models`` subcommand — ``grok models``, ``devin models list --format json`` —
that prints the account's available models to stdout. This module runs that
command on the machine where the CLI and the vendor login live (the host) and
parses stdout into the picker's model-option row shape.

The convention is the same one the Cursor CLI already uses in-repo
(``list_cursor_cli_model_options`` → ``source="cli"``); output format varies per
vendor, so each declares a ``models_format`` naming a parser here.

Best-effort by contract: any failure (binary absent, non-zero exit, unparseable
output) returns ``[]`` so the picker degrades to a free-text model field. The CLI
still launches with its own stored login, so a session can run regardless.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess

from omnigent.acp_cli_harnesses import AcpCliHarness

_logger = logging.getLogger(__name__)

# A models probe is a bounded local command; keep it short so a dropdown open
# never hangs on a wedged CLI. The char cap bounds the text we parse; the
# timeout is the real backstop (capture_output still buffers stdout first).
_TIMEOUT_S = 15.0
_MAX_OUTPUT_CHARS = 2_000_000


def discover_acp_cli_models(harness_id: str, row: AcpCliHarness) -> list[dict]:
    """Run *row*'s declared ``models`` command and parse it into picker rows.

    :param harness_id: Canonical harness id (for logs), e.g. ``"devin"``.
    :param row: The catalog row; ``models_argv`` empty means no discovery.
    :returns: A list of ``{"id", "displayName", …}`` rows, or ``[]`` when the
        row declares no command or the probe fails (never raises).
    """
    if not row.models_argv:
        return []
    parser = _PARSERS.get(row.models_format)
    if parser is None:
        _logger.info("acp-cli[%s] unknown models_format %r", harness_id, row.models_format)
        return []
    try:
        completed = subprocess.run(
            [row.binary, *row.models_argv],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_TIMEOUT_S,
            check=True,
        )
    except Exception as exc:  # noqa: BLE001 — contract: discovery never raises; any failure degrades to []
        # Absent binary, non-zero exit, timeout, or undecodable output — the CLI
        # still launches with its own stored login, so this is a soft miss, not a
        # dead worker. ``errors="replace"`` already prevents the decode case; the
        # broad catch keeps the documented "never raises" contract for the rest.
        _logger.info("acp-cli[%s] model discovery command failed: %s", harness_id, exc)
        return []
    try:
        return parser((completed.stdout or "")[:_MAX_OUTPUT_CHARS])
    except Exception:  # noqa: BLE001 — a bad parse must degrade to free-text, never crash the host
        _logger.info("acp-cli[%s] model output parse failed", harness_id, exc_info=True)
        return []


def _parse_devin_json(out: str) -> list[dict]:
    """Parse ``devin models list --format json`` → picker rows.

    Shape: ``{"families": [{"family_label", "variants": [{"model_uid", "label",
    "max_context_tokens", "max_output_tokens", "cost_tier"}]}]}``. Effort is
    encoded in ``model_uid`` (e.g. ``claude-opus-5-high``), so each variant is one
    selectable row — do not split a separate effort axis for Devin.
    """
    data = json.loads(out)
    rows: list[dict] = []
    for family in data.get("families", []):
        if not isinstance(family, dict):
            continue
        group = family.get("family_label")
        for variant in family.get("variants", []):
            if not isinstance(variant, dict):
                continue
            uid = variant.get("model_uid")
            if not isinstance(uid, str) or not uid:
                continue
            row: dict = {"id": uid, "displayName": variant.get("label") or uid}
            if isinstance(group, str) and group:
                row["group"] = group
            for src, dst in (
                ("max_context_tokens", "context_window"),
                ("max_output_tokens", "max_output_tokens"),
            ):
                if isinstance(variant.get(src), int):
                    row[dst] = variant[src]
            if isinstance(variant.get("cost_tier"), str):
                row["cost_tier"] = variant["cost_tier"]
            rows.append(row)
    return rows


# A bulleted list: `  * grok-4.6 (default)` / `  - grok-4.5`. The id is the first
# whitespace-run after the bullet, so a trailing `(default)` is excluded.
_BULLET_RE = re.compile(r"^\s*[*-]\s+(\S+)")


def _parse_bullet_text(out: str) -> list[dict]:
    """Parse a bulleted ``<cli> models`` text list → picker rows (e.g. ``grok models``)."""
    rows: list[dict] = []
    seen: set[str] = set()
    for line in out.splitlines():
        match = _BULLET_RE.match(line)
        if match is None:
            continue
        model_id = match.group(1)
        if model_id in seen:
            continue
        seen.add(model_id)
        rows.append({"id": model_id, "displayName": model_id})
    return rows


# models_format (declared per catalog row) → parser.
_PARSERS = {
    "devin-json": _parse_devin_json,
    "grok-text": _parse_bullet_text,
}
