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
    videos: list[str] = field(default_factory=list)
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
        stem = self.file_stem()
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
        if self.videos:
            lines[-1:-1] = [
                "## Videos",
                "",
                *(f"- [{Path(v).name}]({v})" for v in self.videos),
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

    def file_stem(self) -> str:
        """File-name-safe stem shared by this run's report and videos."""
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{self.scenario}-{self._param_slug()}")

    def verdict_lines(self) -> list[str]:
        """Short closing summary for a recording: verdict, then each broken check."""
        broken = self.failures + self.stale_gaps
        head = (
            "✗ contract broken"
            if broken
            else ("⚠ known gap" if self.gaps else "✓ all checks passed")
        )
        lines = [f"{self.scenario}  ({self._param_slug()})", head]
        for check in broken + self.gaps:
            gap = f" [{check.known_gap}]" if check.known_gap else ""
            lines.append(f"  ✗ {check.name}{gap}: {check.detail}"[:160])
        return lines


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


def html_index(directory: Path | None = None) -> Path:
    """Write ``index.html`` beside the reports: every run, its checks and videos.

    :param directory: Report directory; defaults to :func:`report_dir`.
    :returns: The written file.
    """
    import html

    root = directory or report_dir()
    sections = []
    for path in sorted(root.glob("*.json")):
        data = json.loads(path.read_text())
        report = ScenarioReport(
            data["scenario"],
            data["params"],
            checks=[Check(**check) for check in data["checks"]],
            videos=data.get("videos", []),
        )
        broken = report.failures + report.stale_gaps
        verdict = "FAIL" if broken else ("gap" if report.gaps else "pass")
        items = "".join(
            f"<li class={'ok' if c.passed else 'bad'}>{html.escape(c.name)}"
            f"{html.escape(f' [{c.known_gap}]') if c.known_gap else ''} "
            f"<small>{html.escape(c.detail)}</small></li>"
            for c in report.checks
        )
        videos = "".join(
            f'<figure><video controls preload=metadata src="{html.escape(_relative(v, root))}">'
            f"</video><figcaption>{html.escape(Path(v).name)}</figcaption></figure>"
            for v in report.videos
        )
        params = ", ".join(f"{k}={v}" for k, v in report.params.items())
        sections.append(
            f"<section class={verdict}><h2>{html.escape(report.scenario)} "
            f"<small>{html.escape(params)}</small> <b>{verdict}</b></h2>"
            f"<ul>{items}</ul><div class=videos>{videos}</div></section>"
        )
    page = (
        "<!doctype html><meta charset=utf-8><title>Resilience runs</title><style>"
        "body{font:14px system-ui;margin:24px} section{border:1px solid #ddd;border-radius:8px;"
        "padding:8px 16px;margin:12px 0} section.FAIL{border-color:#b91c1c}"
        " section.gap{border-color:#d97706}"
        " .ok{color:#15803d} .bad{color:#b91c1c} .videos{display:flex;gap:12px;flex-wrap:wrap}"
        " video{width:620px} small{color:#555}</style><h1>Resilience runs</h1>" + "".join(sections)
    )
    target = root / "index.html"
    target.write_text(page)
    return target


def _relative(path: str, root: Path) -> str:
    target = Path(path)
    return target.relative_to(root).as_posix() if target.is_relative_to(root) else path


if __name__ == "__main__":
    import sys

    if "--html" in sys.argv[1:]:
        print(html_index())
    else:
        print(matrix())
