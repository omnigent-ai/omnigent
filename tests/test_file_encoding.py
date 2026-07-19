"""Encoding-policy guards for omnigent-owned text I/O.

Windows defaults text I/O to cp1252, so an ``open``/``read_text``/``write_text``
without an explicit ``encoding`` mojibakes UTF-8 content (e.g. an em-dash in an
agent prompt). Three complementary guards:

1. A **receiver-independent** AST scan (``tests/_encoding_scan.py``) — matches the
   call by name, so it catches cases Ruff's ``PLW1514`` can't infer (e.g.
   ``Path | None`` receivers). This is the primary structural guard; it runs
   cross-platform.
2. A **runtime** ``EncodingWarning`` net around real loading.
3. A **windows_only** end-to-end regression under a forced cp1252 default.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests._encoding_scan import ALLOWLIST, scan_package


def test_no_unencoded_owned_text_io() -> None:
    """Structural guard: no omnigent-owned open/read_text/write_text may omit an
    explicit ``encoding`` (receiver-independent; catches what PLW1514 misses).
    """
    pkg = Path(__file__).resolve().parents[1] / "omnigent"
    violations = scan_package(pkg, ALLOWLIST)
    assert not violations, "Unencoded text I/O (add encoding=...):\n" + "\n".join(violations)


def test_agent_loading_emits_no_encoding_warning(tmp_path: Path) -> None:
    """Runtime net: loading an agent config under
    ``-X warn_default_encoding -W error::EncodingWarning`` trips no
    unspecified-encoding read — regardless of the CI host's locale.
    """
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "config.yaml").write_text(
        'spec_version: 1\nname: t\nprompt: "plans — not code ↔ review"\n',
        encoding="utf-8",
    )
    script = textwrap.dedent(
        f"""
        from pathlib import Path
        from omnigent.spec.parser import parse
        spec = parse(Path(r{str(agent)!r}))
        assert spec.instructions and "—" in spec.instructions
        """
    )
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "warn_default_encoding",
            "-W",
            "error::EncodingWarning",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.windows_only
def test_config_read_forces_utf8_under_cp1252_default(tmp_path: Path) -> None:
    """End-to-end: with the interpreter default FORCED to cp1252 (PYTHONUTF8=0 on
    Windows), parse() still reads the UTF-8 config correctly because the read
    names an explicit encoding. The child first proves cp1252 is in effect (an
    unencoded read of the same file mojibakes), then proves parse() does not.
    """
    agent = tmp_path / "agent"
    agent.mkdir()
    cfg = agent / "config.yaml"
    cfg.write_text('spec_version: 1\nname: t\nprompt: "itself — it plans"\n', encoding="utf-8")
    script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        assert not sys.flags.utf8_mode, "expected PYTHONUTF8=0"
        # Unencoded read mojibakes under the cp1252 default (proves the scenario).
        assert "â€" in Path(r{str(cfg)!r}).read_text()
        # parse() names encoding='utf-8', so it must NOT mojibake.
        from omnigent.spec.parser import parse
        spec = parse(Path(r{str(agent)!r}))
        assert "itself — it plans" in (spec.instructions or ""), repr(spec.instructions)
        """
    )
    env = dict(os.environ)
    env["PYTHONUTF8"] = "0"
    env.pop("PYTHONIOENCODING", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert result.returncode == 0, result.stderr
