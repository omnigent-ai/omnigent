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

from tests._encoding_scan import ALLOWLIST, find_violations, scan_package


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
    """End-to-end: with the interpreter default at cp1252 (PYTHONUTF8=0 on a
    Windows host whose ANSI code page is 1252), parse() still reads the UTF-8
    config correctly because the read names an explicit encoding.

    The child asserts the default codec is actually ``cp1252`` — PYTHONUTF8=0
    only disables UTF-8 *mode*; the resulting default is the host ANSI code page
    (Python has no per-process override for it), so the assertion pins the
    scenario to a real cp1252 default rather than assuming it. It then proves an
    unencoded read of the same file mojibakes, and that parse() does not.
    """
    agent = tmp_path / "agent"
    agent.mkdir()
    cfg = agent / "config.yaml"
    cfg.write_text('spec_version: 1\nname: t\nprompt: "itself — it plans"\n', encoding="utf-8")
    script = textwrap.dedent(
        f"""
        import locale
        import sys
        from pathlib import Path
        assert not sys.flags.utf8_mode, "expected PYTHONUTF8=0 (UTF-8 mode off)"
        enc = locale.getpreferredencoding(False)
        assert enc.lower() == "cp1252", f"expected a cp1252 default, got {{enc!r}}"
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


# Snippets the scanner MUST flag — one per detection path. Kept alongside the
# clean cases so the guard can't silently weaken (a broken detector fails here
# long before it lets a real omnigent regression through).
_FLAGGED = [
    'open("f")',
    'from pathlib import Path\nPath("f").read_text()',
    'from pathlib import Path\nPath("f").write_text("x")',
    'import os\nos.fdopen(fd, "w")',
    "import configparser\ncfg = configparser.ConfigParser()\ncfg.read(p)",
    'run("import os; data = open(p).read()")',  # embedded, plain string
    'run(f"exec(open({p!r}).read())")',  # embedded, f-string
]
_CLEAN = [
    'open("f", encoding="utf-8")',
    'open("f", "rb")',  # binary
    'open("f", encoding="locale")',
    'from pathlib import Path\nPath("f").read_text(encoding="utf-8")',
    'import tarfile\ntarfile.open("f")',  # archive, not text
    'import os\nos.open("f", 0)',  # low-level fd
    'import os\nos.fdopen(fd, "wb")',  # binary
    'import os\nos.fdopen(fd, "w", encoding="utf-8")',
    "import configparser\ncfg = configparser.ConfigParser()\ncfg.read(p, encoding='locale')",
    "run(\"open(p, encoding='utf-8')\")",  # embedded, encoded
    'run("call open() to begin; then stop.")',  # prose: not parseable Python
    'run("// import fs;\\nfs.open(p);")',  # generated JS: not parseable Python
]


@pytest.mark.parametrize("src", _FLAGGED)
def test_scanner_flags_unencoded(src: str) -> None:
    assert find_violations(src), f"expected a violation for:\n{src}"


@pytest.mark.parametrize("src", _CLEAN)
def test_scanner_accepts_encoded_and_nonfile(src: str) -> None:
    assert not find_violations(src), f"unexpected violation for:\n{src}"
