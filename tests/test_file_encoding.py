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

from tests._encoding_scan import ALLOWLIST, find_violations, owned_packages, scan_package


def test_no_unencoded_owned_text_io() -> None:
    """Structural guard: no owned open/read_text/write_text/os.fdopen/
    ConfigParser.read may omit an explicit ``encoding`` (receiver-independent;
    catches what PLW1514 misses). Covers every first-party Python package shipped
    from this repo (the ``omnigent`` app and the ``omnigent_client`` /
    ``omnigent_ui_sdk`` SDKs).
    """
    packages = owned_packages()
    for pkg in packages:
        # Fail loudly if a configured root moved or emptied — otherwise the guard
        # would silently pass by scanning nothing.
        assert pkg.is_dir(), f"configured package root is missing: {pkg}"
        assert any(pkg.rglob("*.py")), f"configured package root has no .py files: {pkg}"
    violations = [v for pkg in packages for v in scan_package(pkg, ALLOWLIST)]
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
    'open("f", encoding=None)',  # encoding=None == default codec, not a fix
    'from pathlib import Path\nPath("f").read_text()',
    'from pathlib import Path\nPath("f").write_text("x")',
    'import os\nos.fdopen(fd, "w")',
    'from os import fdopen\nfdopen(fd, "w")',  # imported fdopen
    'from os import fdopen as fdo\nfdo(fd, "w")',  # aliased fdopen
    'import os as _o\n_o.fdopen(fd, "w")',  # aliased os module
    "import configparser\ncfg = configparser.ConfigParser()\ncfg.read(p)",
    "from configparser import ConfigParser as CP\nc = CP()\nc.read(p)",  # aliased ctor
    "self.cfg = ConfigParser()\nself.cfg.read(p)",  # attribute-bound instance
    'import gzip\ngzip.open(p, "rt")',  # gzip text mode needs encoding
    'import bz2\nbz2.open(p, "wt")',  # bz2 text mode needs encoding
    'from gzip import open as gopen\ngopen(p, "rt")',  # aliased gzip.open, text
    'import gzip as gz\ngz.open("blob.gz", "rt")',  # aliased module (filename has 'b')
    'import gzip\ngzip.open(p, "r" + "t")',  # computed mode, not provably binary
    "import gzip\ngzip.open(p, mode)",  # dynamic mode, not provably binary
    'from pathlib import Path\ngzip = Path("x")\ngzip.open("rt")',  # rebound name: real text open
    'run("import os; data = open(p).read()")',  # embedded, plain string
    'run(f"exec(open({p!r}).read())")',  # embedded, f-string
    'run("""\n    import os\n    data = open(p).read()\n""")',  # embedded, indented
]
_CLEAN = [
    'open("f", encoding="utf-8")',
    'open("f", "rb")',  # binary
    'open("f", encoding="locale")',
    'open("f", encoding=enc)',  # a real (non-None) encoding expression
    'from pathlib import Path\nPath("f").read_text(encoding="utf-8")',
    'import tarfile\ntarfile.open("f")',  # archive, not text
    'import os\nos.open("f", 0)',  # low-level fd
    'import os\nos.fdopen(fd, "wb")',  # binary
    'import os\nos.fdopen(fd, "w", encoding="utf-8")',
    'from os import fdopen as fdo\nfdo(fd, "wb")',  # aliased fdopen, binary
    "import gzip\ngzip.open(p)",  # gzip default binary
    'import gzip\ngzip.open(p, "rb")',  # gzip explicit binary
    'import gzip\ngzip.open(p, "rt", encoding="utf-8")',  # gzip text + encoding
    'import lzma\nlzma.open(p, "wb")',  # lzma binary
    "import gzip as gz\ngz.open(p)",  # aliased module, default binary
    'import gzip as gz\ngz.open(p, "rb")',  # aliased module, binary
    "import configparser\ncfg = configparser.ConfigParser()\ncfg.read(p, encoding='locale')",
    "from configparser import ConfigParser as CP\nc = CP()\nc.read(p, encoding='utf-8')",
    "self.cfg = ConfigParser()\nself.cfg.read(p, encoding='utf-8')",  # attr-bound, encoded
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


def _run_utf8_mode(script: str) -> subprocess.CompletedProcess[str]:
    """Run *script* in a child with UTF-8 Mode ON (Python 3.15's default)."""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


@pytest.mark.windows_only
def test_edit_preserves_cp1252_under_utf8_mode(tmp_path: Path) -> None:
    """Editing a cp1252 file under UTF-8 Mode, INSERTING non-ASCII, must not mix
    encodings.

    ``getpreferredencoding(False)`` returns ``"utf-8"`` in UTF-8 Mode even on a
    cp1252 host, so a naive fallback reads the existing ``é`` as a surrogate and
    writes the whole result UTF-8 — leaving one file with cp1252 ``\\xe9`` and a
    UTF-8 ``ï`` (``\\xc3\\xaf``) side by side. The coding tools fall back to
    ``locale.getencoding()`` (the true cp1252), so both stay cp1252.

    The assertion is on the exact bytes: a no-op or the old broken fallback both
    diverge from ``b"caf\\xe9 na\\xefve\\r\\n"`` (an ASCII->ASCII edit would not).
    """
    f = tmp_path / "latin.txt"
    f.write_bytes("caf\N{LATIN SMALL LETTER E WITH ACUTE} old\n".encode("cp1252"))
    result = _run_utf8_mode(
        f"""
        import sys, locale
        from pathlib import Path
        assert sys.flags.utf8_mode, "expected PYTHONUTF8=1"
        assert locale.getpreferredencoding(False).lower() == "utf-8"  # the trap
        assert locale.getencoding().lower() == "cp1252"               # the truth
        from omnigent.client_tools.coding import Edit
        edit = getattr(Edit, "func", None) or getattr(Edit, "__wrapped__", Edit)
        msg = edit(file_path=r{str(f)!r}, old_string="old", new_string="na\\u00efve")
        assert "Replaced" in msg, msg
        raw = Path(r{str(f)!r}).read_bytes()
        assert raw == b"caf\\xe9 na\\xefve\\r\\n", raw
        """
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.windows_only
def test_detect_encoding_reads_utf8_config_under_utf8_mode(tmp_path: Path) -> None:
    """A UTF-8 vendor config must read back correctly under UTF-8 Mode, and a
    cp1252 one must still be detected as cp1252 (not the lying preferred codec).
    """
    utf8_cfg = tmp_path / "utf8"
    utf8_cfg.write_text("[p]\nhost = café\n", encoding="utf-8")
    latin_cfg = tmp_path / "latin"
    latin_cfg.write_bytes("[p]\nhost = café\n".encode("cp1252"))
    result = _run_utf8_mode(
        f"""
        import configparser
        from pathlib import Path
        from omnigent._encoding import detect_encoding
        u, latin = Path(r{str(utf8_cfg)!r}), Path(r{str(latin_cfg)!r})
        assert detect_encoding(u) == "utf-8"
        assert detect_encoding(latin).lower() == "cp1252"
        c = configparser.ConfigParser()
        c.read(u, encoding=detect_encoding(u))
        assert c["p"]["host"] == "café", c["p"]["host"]
        """
    )
    assert result.returncode == 0, result.stderr


# ``cli._read_existing_cfg`` is the guard that keeps the Databricks rewrite flow
# from replacing an original it failed to read. (The full ``_isolated_databricks_
# cfg`` context manager pulls in a Databricks-internal module not shipped here, so
# the guard itself is tested directly.)
def test_read_existing_cfg_missing_is_noop(tmp_path: Path) -> None:
    """A missing original is the new-config case: no read, no error."""
    import configparser

    from omnigent.cli import _read_existing_cfg

    cfg = configparser.ConfigParser()
    _read_existing_cfg(cfg, tmp_path / "absent.cfg", "utf-8")
    assert cfg.sections() == []


def test_read_existing_cfg_loads_present_file(tmp_path: Path) -> None:
    """An existing, readable original is loaded (so it can be preserved)."""
    import configparser

    from omnigent.cli import _read_existing_cfg

    original = tmp_path / ".databrickscfg"
    original.write_text("[legacy]\nhost = https://keep.example\n", encoding="utf-8")
    cfg = configparser.ConfigParser()
    _read_existing_cfg(cfg, original, "utf-8")
    assert cfg["legacy"]["host"] == "https://keep.example"


def test_read_existing_cfg_unreadable_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing file that ``ConfigParser.read`` silently skips (a permission
    or transient I/O error) must raise, so the rewrite flow aborts instead of
    seeding an empty config and overwriting the original — which would drop the
    user's ``[legacy]`` section. Nothing is captured, so a caller cannot proceed.
    """
    import configparser

    from omnigent.cli import _read_existing_cfg

    original = tmp_path / ".databrickscfg"
    original.write_text("[legacy]\nhost = https://keep.example\n", encoding="utf-8")
    monkeypatch.setattr(configparser.ConfigParser, "read", lambda self, *a, **k: [])

    cfg = configparser.ConfigParser()
    with pytest.raises(OSError):
        _read_existing_cfg(cfg, original, "utf-8")
    assert cfg.sections() == []
