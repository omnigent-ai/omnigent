"""Collect a scenario's checks, write them out, then fail with every broken expectation.

A scenario records each UX-contract expectation as a :class:`Check` instead
of asserting one at a time, so a run reports all of them (the matrix needs
the whole row) together with the session timeline that explains them.
Reports are kept under ``OMNIGENT_RESILIENCE_REPORT_DIR``, by default
``.omnigent/resilience/`` in the checkout, after the lab root is deleted.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tests.e2e.resilience.lab.observe import SessionWatcher

_REPO_ROOT = Path(__file__).resolve().parents[4]
REPORT_DIR_ENV = "OMNIGENT_RESILIENCE_REPORT_DIR"


@dataclass(frozen=True)
class Check:
    """One expectation from the UX contract.

    :param name: Short stable name, e.g. ``"no_failed_status_during_outage"``.
    :param passed: Whether the expectation held.
    :param detail: Evidence, e.g. the offending observation.
    :param known_gap: Finding id this check is known to fail on, e.g. ``"R5"``.
        A failing known-gap check is reported but does not fail the run; a
        passing one does, so the stale marker gets removed with the fix.
    :param intermittent: The known gap shows up only on some runs (a race), so
        passing does not make its marker stale.
    """

    name: str
    passed: bool
    detail: str = ""
    known_gap: str | None = None
    intermittent: bool = False


@dataclass
class ScenarioReport:
    """Checks and evidence for one scenario run.

    :param scenario: Scenario id and title, e.g. ``"S2 server restart"``.
    :param params: Run parameters, e.g. ``{"phase": "idle", "outage_s": 5}``.
    """

    scenario: str
    params: dict[str, Any]
    checks: list[Check] = field(default_factory=list)
    timeline: str = ""
    lab_root: str = ""
    started: float = field(default_factory=time.time)

    def check(
        self,
        name: str,
        passed: bool,
        detail: str = "",
        *,
        known_gap: str | None = None,
        intermittent: bool = False,
    ) -> bool:
        """Record one expectation; returns *passed* for chaining.

        :param known_gap: Finding id when this check is expected to fail today.
        :param intermittent: The known gap is a race that fails only on some runs.
        """
        self.checks.append(Check(name, bool(passed), detail, known_gap, intermittent))
        return bool(passed)

    def attach(self, watcher: SessionWatcher, lab_root: Path) -> None:
        """Attach the session timeline and lab location as evidence."""
        self.timeline = watcher.describe(self.started)
        self.lab_root = str(lab_root)

    @property
    def failures(self) -> list[Check]:
        """Checks that did not hold, excluding known gaps."""
        return [c for c in self.checks if not c.passed and c.known_gap is None]

    @property
    def gaps(self) -> list[Check]:
        """Known-gap checks that still fail."""
        return [c for c in self.checks if not c.passed and c.known_gap is not None]

    @property
    def stale_gaps(self) -> list[Check]:
        """Known-gap checks that now pass; their markers must be removed."""
        return [
            c for c in self.checks if c.passed and c.known_gap is not None and not c.intermittent
        ]

    def markdown_row(self) -> str:
        """One matrix row: scenario, parameters, verdict, failed checks."""
        params = ", ".join(f"{key}={value}" for key, value in self.params.items())
        broken = self.failures + self.stale_gaps
        verdict = "FAIL" if broken else ("gap" if self.gaps else "pass")
        failed = "; ".join(
            f"{c.name}{f' [{c.known_gap}]' if c.known_gap else ''}: {c.detail}"
            for c in broken + self.gaps
        )
        return f"| {self.scenario} | {params} | {verdict} | {failed or '—'} |"

    def write(self) -> Path:
        """Write ``<name>.json`` and ``<name>.md`` to the report directory."""
        directory = report_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{self.scenario}-{self._param_slug()}")
        payload = {**asdict(self), "failures": [asdict(c) for c in self.failures]}
        (directory / f"{stem}.json").write_text(json.dumps(payload, indent=2) + "\n")
        lines = [
            f"# {self.scenario} ({self._param_slug()})",
            "",
            "| Scenario | Parameters | Verdict | Failed checks |",
            "| --- | --- | --- | --- |",
            self.markdown_row(),
            "",
            "## Checks",
            "",
            *(
                f"- {'✅' if c.passed else ('⚠️' if c.known_gap else '❌')} `{c.name}`"
                f"{f' (known gap {c.known_gap})' if c.known_gap else ''} {c.detail}".rstrip()
                for c in self.checks
            ),
            "",
            "## Timeline",
            "",
            "```text",
            self.timeline,
            "```",
            "",
        ]
        path = directory / f"{stem}.md"
        path.write_text("\n".join(lines))
        return path

    def require(self) -> None:
        """Fail with every broken expectation and the timeline that explains it."""
        path = self.write()
        broken = self.failures + self.stale_gaps
        if not broken:
            return
        listed = "\n".join(
            f"  - {c.name}: "
            + (f"passes now; remove known_gap={c.known_gap!r}" if c.passed else c.detail)
            for c in broken
        )
        raise AssertionError(
            f"{self.scenario} {self._param_slug()} broke {len(broken)} expectation(s):\n"
            f"{listed}\nreport: {path}\ntimeline:\n{self.timeline}"
        )

    def _param_slug(self) -> str:
        return ",".join(f"{key}={value}" for key, value in self.params.items())


def report_dir() -> Path:
    """Where scenario reports are written."""
    return Path(os.environ.get(REPORT_DIR_ENV, _REPO_ROOT / ".omnigent" / "resilience"))


def matrix(directory: Path | None = None) -> str:
    """Render every saved report in *directory* as one markdown matrix.

    :param directory: Report directory; defaults to :func:`report_dir`.
    :returns: A markdown table, one row per scenario run, sorted by scenario.
    """
    rows = []
    for path in sorted((directory or report_dir()).glob("*.json")):
        data = json.loads(path.read_text())
        report = ScenarioReport(
            data["scenario"],
            data["params"],
            checks=[Check(**check) for check in data["checks"]],
        )
        rows.append(report.markdown_row())
    header = ["| Scenario | Parameters | Verdict | Failed checks |", "| --- | --- | --- | --- |"]
    return "\n".join(header + rows)


if __name__ == "__main__":
    print(matrix())
