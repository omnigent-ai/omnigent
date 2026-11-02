"""Reconcile reliability dimension queries with an exported dashboard's headline policy."""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any

_VERSION_MEMBERSHIP = """kpi_session_versions AS (
SELECT v.session_id, v.harness,
    CASE WHEN e.session_id IS NOT NULL THEN 1 ELSE 0 END AS is_error,
    e.first_included_error_time, v.runner_version, v.server_version
FROM selected_facts v
LEFT JOIN window_included e
    ON e.session_id = v.session_id AND e.window_name = 'last_7d'
WHERE v.record_type = 'version_session' AND v.report_days = 7
),
"""


def _before_cte(query: str, name: str) -> tuple[str, str]:
    """Locate one supported CTE boundary, refusing missing or ambiguous shapes."""
    matches = list(re.finditer(r"(?m)^" + re.escape(name) + r"\s+AS\s*\(", query))
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {name} CTE; found {len(matches)}")
    offset = matches[0].start()
    return query[:offset], query[offset:]


def reconcile_dimensions(
    exported: dict[str, Any],
    *,
    headline: str = "mv_compare_kpi3_errors",
    dimensions: str = "mv_compare_kpi3_dimension_rates",
) -> dict[str, Any]:
    """Replace only dimension error membership; preserve output groups and metadata."""
    if headline == dimensions:
        raise ValueError("Headline and dimension datasets must differ")
    result = copy.deepcopy(exported)
    raw = result.get("serialized_dashboard")
    document = json.loads(raw) if isinstance(raw, str) else result
    datasets = document.get("datasets")
    if not isinstance(datasets, list):
        raise ValueError("Expected an exported dashboard with datasets")

    def dataset(name: str) -> dict[str, Any]:
        found = [item for item in datasets if isinstance(item, dict) and item.get("name") == name]
        if len(found) != 1:
            raise ValueError(f"Expected exactly one dataset named {name}")
        return found[0]

    def query(item: dict[str, Any]) -> str:
        lines = item.get("queryLines")
        if not isinstance(lines, list) or not lines or not all(isinstance(x, str) for x in lines):
            raise ValueError("Expected nonempty queryLines containing SQL strings")
        return "".join(lines)

    source = query(dataset(headline))
    target = dataset(dimensions)
    prefix, _ = _before_cte(source, "window_rates")
    # These stages carry the headline's current filters, exclusions and window membership.
    for stage in ("selected_facts", "window_included"):
        _before_cte(prefix, stage)
    _, aggregation = _before_cte(query(target), "kpi_dimension_rates")
    target["queryLines"] = (prefix + _VERSION_MEMBERSHIP + aggregation).splitlines(keepends=True)
    if isinstance(raw, str):
        result["serialized_dashboard"] = json.dumps(document)
    return result


def main() -> None:
    """Write a reviewable local export; publishing remains an explicit operator action."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--headline", default="mv_compare_kpi3_errors")
    parser.add_argument("--dimensions", default="mv_compare_kpi3_dimension_rates")
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("Use a separate output file to preserve the original export")
    try:
        result = reconcile_dimensions(
            json.loads(args.input.read_text()), headline=args.headline, dimensions=args.dimensions
        )
    except (ValueError, TypeError, AttributeError) as exc:
        parser.error(str(exc))
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    main()
