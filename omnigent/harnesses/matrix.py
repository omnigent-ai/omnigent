"""Render the harness x capability matrix from the registry.

The registry is the single source of truth; this module turns it into a
human-readable table so "what does each harness support?" can be answered at a
glance (and, later, checked into docs / surfaced via ``omni harness matrix``).
"""

from __future__ import annotations

from omnigent.harnesses.registry import all_descriptors

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Harness", ""),
    ("Integration", "integration_mode"),
    ("Elicitation", "elicitation"),
    ("Resume", "resume"),
    ("Model family", "model_family"),
    ("Effort", "effort"),
    ("Auth", "auth"),
    ("Model override", ""),
    ("Sub-agents", ""),
)


def _cell(descriptor: object, header: str, attr: str) -> str:
    if header == "Harness":
        return descriptor.name  # type: ignore[attr-defined]
    if header == "Model override":
        return "yes" if descriptor.supports_model_override else "no"  # type: ignore[attr-defined]
    if header == "Sub-agents":
        return "yes" if descriptor.capabilities.subagents else "no"  # type: ignore[attr-defined]
    value = getattr(descriptor.capabilities, attr)  # type: ignore[attr-defined]
    # Enum -> its str value; leave plain values untouched.
    return getattr(value, "value", str(value))


def render_matrix() -> str:
    """Return the harness x capability matrix as a GitHub-flavored markdown table."""
    headers = [header for header, _ in _COLUMNS]
    rows: list[list[str]] = []
    for descriptor in all_descriptors():
        rows.append([_cell(descriptor, header, attr) for header, attr in _COLUMNS])

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _fmt(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    lines = [
        _fmt(headers),
        "| " + " | ".join("-" * widths[i] for i in range(len(headers))) + " |",
    ]
    lines.extend(_fmt(row) for row in rows)
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    print(render_matrix())
