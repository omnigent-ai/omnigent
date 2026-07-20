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
    'open("f", "r", -1, None)',  # positional encoding=None is still default
    'open(*args, "rb")',  # star expansion shifts the apparent mode position
    'open("f", "r", *args, "utf-8")',  # star shifts the apparent encoding
    'from pathlib import Path\nPath("f").read_text()',
    'from pathlib import Path\nPath("f").read_text(None)',
    'from pathlib import Path\nPath("f").write_text("x")',
    'from pathlib import Path\nPath("f").write_text("x", None)',
    'from pathlib import Path\nPath("f").open("r", -1, None)',
    'import os\nos.fdopen(fd, "w")',
    'import os\nos.fdopen(fd, "r", -1, None)',
    'from os import fdopen\nfdopen(fd, "w")',  # imported fdopen
    'from os import fdopen as fdo\nfdo(fd, "w")',  # aliased fdopen
    'import os as _o\n_o.fdopen(fd, "w")',  # aliased os module
    'import os\nfdo = os.fdopen\nfdo(fd, "w")',  # assigned fdopen alias
    'fopen = open\nfopen("f")',  # assigned builtin-open alias
    "import configparser\ncfg = configparser.ConfigParser()\ncfg.read(p)",
    "import configparser\ncfg = configparser.ConfigParser()\ncfg.read(p, None)",
    "from configparser import ConfigParser as CP\nc = CP()\nc.read(p)",  # aliased ctor
    (
        'import configparser\nCP = configparser.ConfigParser\ncfg = CP()\ncfg.read("f")'
    ),  # assigned constructor alias
    (
        'import configparser\ncfg = configparser.ConfigParser()\nother = cfg\nother.read("f")'
    ),  # assigned instance alias
    "self.cfg = ConfigParser()\nself.cfg.read(p)",  # attribute-bound instance
    'import gzip\ngzip.open(p, "rt")',  # gzip text mode needs encoding
    'import gzip\ngzip.open(p, "rt", 9, None)',  # positional None is not a codec
    'import bz2\nbz2.open(p, "wt")',  # bz2 text mode needs encoding
    'import lzma\nlzma.open(p, "rt")',  # lzma encoding is keyword-only
    'from gzip import open as gopen\ngopen(p, "rt")',  # aliased gzip.open, text
    'import gzip as gz\ngz.open("blob.gz", "rt")',  # aliased module (filename has 'b')
    'import gzip\ngzip.open(p, "r" + "t")',  # computed mode, not provably binary
    "import gzip\ngzip.open(p, mode)",  # dynamic mode, not provably binary
    'from pathlib import Path\ngzip = Path("x")\ngzip.open("rt")',  # rebound name: real text open
    'from pathlib import Path\nimport gzip\ngzip = Path("x")\ngzip.open()',  # rebound module
    'def a():\n    from gzip import open\ndef b():\n    open("plain.txt")',  # nested, no taint
    (
        'from gzip import open as gopen\ngopen(io.BytesIO(), "rt")\ngopen = object()'
    ),  # later function-alias rebind must not erase provenance
    (
        'import gzip\ngzip.open("blob.gz", "rt")\ndef later():\n    gzip = Path(\'x\')'
    ),  # other-scope assignment must fail closed (filename contains "b")
    (
        "import gzip\nimport io\ndef consume(gzip=io):\n    gzip.open(os.devnull)"
    ),  # parameter shadow
    "import gzip\nimport io as gzip\ngzip.open(os.devnull)",  # module import rebind
    (
        "from gzip import open as gopen\nfrom io import open as gopen\ngopen(os.devnull)"
    ),  # function import rebind
    'import gzip\ngz = gzip\ngz.open("blob.gz", "rt")',  # assigned module alias
    'import gzip\ngopen = gzip.open\ngopen(p, "rt")',  # assigned function alias
    'import gzip\n(gz,) = (gzip,)\ngz.open("blob.gz", "rt")',  # unpacked module alias
    'import gzip\n(gopen,) = (gzip.open,)\ngopen(p, "rt")',  # unpacked function alias
    'import gzip\ngz = gzip if cond else gzip\ngz.open(p, "rt")',  # conditional alias
    'import gzip\ngzip.open(path, **{"mode": "rt"})',  # **kwargs hides a text mode
    'import gzip\ngzip.open(*[path, "rt"])',  # *args hides a text mode
    "open(*args)",  # dynamic args: can't prove binary
    "open(**kwargs)",  # dynamic kwargs: can't prove binary/encoding
    'import io\nio.open("blob.txt")',  # module-style filename is not a Path mode
    'import io\nio.open("rb")',  # valid-looking filename; io mode is argument 1
    'import io\nio.open(p, "r", -1, None)',  # positional None still defaults
    'import io as streamio\nstreamio.open("wb")',  # aliased text-opener module
    'from io import open as iopen\niopen("rb")',  # aliased text-opener function
    'import io\nbox.open = io.open\nbox.open(p, "r", -1)',  # unknown receiver signature
    'import io\nbox.open = io.open\nbox.open("rb")',  # filename is not a bound mode
    "dist.read_text(name)",  # unknown read_text signature: positional arg is not a codec
    "thing.write_text(data, enc)",  # unknown write_text signature
    "import codecs\ncodecs.open(p)",  # codecs defaults to locale text in mode r
    'import codecs\ncodecs.open(p, "r")',
    'import codecs\ncodecs.open("rb")',  # codecs filename, default text mode
    'import codecs\ncodecs.open(p, "r", None)',  # None still uses the locale
    ('from codecs import open\nopen(p, "r", None, "utf-8")'),  # arg 3 is errors, not encoding
    (
        'import codecs\nopen = codecs.open\nopen(p, "r", None, "utf-8")'
    ),  # rebound bare name has an ambiguous positional signature
    (
        'import codecs\nbox.fdopen = codecs.open\nbox.fdopen(p, "r", None, "utf-8")'
    ),  # unknown fdopen receiver: argument 3 may not be its encoding
    'from builtins import open as bopen\nbopen("rb")',  # filename, not a mode
    'import io\n(streamio,) = (io,)\nstreamio.open("rb")',  # unpacked module alias
    'import io\n(iopen,) = (io.open,)\niopen("rb")',  # unpacked function alias
    'import io\nstreamio = io if cond else io\nstreamio.open("rb")',
    'from pathlib import Path\ntarfile = Path("plain.txt")\ntarfile.open("r")',
    'import tarfile\nfrom pathlib import Path\ntarfile = Path("x")\ntarfile.open("r")',
    'run("import os; data = open(p).read()")',  # embedded, plain string
    'run(f"exec(open({p!r}).read())")',  # embedded, f-string
    'run("""\n    import os\n    data = open(p).read()\n""")',  # embedded, indented
    (
        'run("""import gzip as gz\ngz.open("blob.gz", "rt")\ndef later():\n    gz = object()\n""")'
    ),  # embedded binding ambiguity
    (
        'run("""import io\nbox.open = io.open\nbox.open(p, "r", -1)\n""")'
    ),  # embedded unknown receiver
    (
        'run("""import io\nbox.open = io.open\nbox.open("rb")\n""")'
    ),  # embedded positional-mode ambiguity
    (
        'run("""import codecs\nbox.fdopen = codecs.open\nbox.fdopen(p, "r", None, "utf-8")\n""")'
    ),  # embedded unknown fdopen receiver
    "run(\"import gzip; gzip.open (p, 'rt')\")",  # legal whitespace before call
]
_CLEAN = [
    'open("f", encoding="utf-8")',
    'open("f", "rb")',  # binary
    'open("f", encoding="locale")',
    'open("f", encoding=enc)',  # a real (non-None) encoding expression
    'open("f", "r", -1, "utf-8")',  # builtin positional encoding
    'from builtins import open as bopen\nbopen("f", "r", -1, "utf-8")',
    'from pathlib import Path\nPath("f").read_text(encoding="utf-8")',
    'import tarfile\ntarfile.open("f")',  # archive, not text
    'import tarfile as tf\ntf.open("archive.tar")',  # proven archive-module alias
    'import os\nos.open("f", 0)',  # low-level fd
    'import os\nos.fdopen(fd, "wb")',  # binary
    'import os\nos.fdopen(fd, "w", encoding="utf-8")',
    'from pathlib import Path\nPath("f").open(mode="rb")',  # receiver-independent binary mode
    'import io\nio.open("f", "rb")',  # io.open's mode is positional argument 1
    'import io\nio.open("f", "r", -1, "utf-8")',
    'import io as streamio\nstreamio.open("f", "wb")',
    'from io import open as iopen\niopen("f", "rb")',
    'from io import open as iopen\niopen("f", "r", -1, "utf-8")',
    'import codecs\ncodecs.open("f", "r", "utf-8")',
    'import codecs\ncodecs.open("f", "rb")',
    'from codecs import open as copen\ncopen("f", "r", "utf-8")',
    'from codecs import open\nopen("f", "rb")',
    'from codecs import open\nopen("f", "r", encoding="utf-8")',
    'from os import fdopen as fdo\nfdo(fd, "wb")',  # aliased fdopen, binary
    'import os\nfdo = os.fdopen\nfdo(fd, "wb")',  # assigned fdopen alias, binary
    'fopen = open\nfopen("f", encoding="utf-8")',
    "import gzip\ngzip.open(p)",  # gzip default binary
    'import gzip\ngzip.open(p, "r")',  # compressed plain r is binary
    'import gzip\ngzip.open(p, "w")',  # compressed plain w is binary
    'import gzip\ngzip.open(p, "rb")',  # gzip explicit binary
    "import bz2\nbz2.open(p)",  # bz2 default binary
    'import bz2\nbz2.open(p, "rb")',  # bz2 explicit binary
    'import gzip\ngzip.open(p, "rt", encoding="utf-8")',  # gzip text + encoding
    'import gzip\ngzip.open(p, "rt", 9, "utf-8")',  # gzip positional encoding
    'import bz2\nbz2.open(p, "wt", 9, "utf-8")',  # bz2 positional encoding
    'import lzma\nlzma.open(p, "wb")',  # lzma binary
    'import lzma\nlzma.open(p, "rt", encoding="utf-8")',
    "import gzip as gz\ngz.open(p)",  # aliased module, default binary
    'import gzip as gz\ngz.open(p, "rb")',  # aliased module, binary
    "import configparser\ncfg = configparser.ConfigParser()\ncfg.read(p, encoding='locale')",
    "from configparser import ConfigParser as CP\nc = CP()\nc.read(p, encoding='utf-8')",
    (
        "import configparser\nCP = configparser.ConfigParser\n"
        "cfg = CP()\nother = cfg\nother.read('f', encoding='utf-8')"
    ),
    "self.cfg = ConfigParser()\nself.cfg.read(p, encoding='utf-8')",  # attr-bound, encoded
    "run(\"open(p, encoding='utf-8')\")",  # embedded, encoded
    'run("call open() to begin; then stop.")',  # prose: not parseable Python
    'run("// import fs;\\nfs.open(p);")',  # generated JS: not parseable Python
    (
        'import gzip\ngzip.open(p, "rt", encoding="utf-8")\ndef later():\n    gzip = Path(\'x\')'
    ),  # ambiguity is accepted when an explicit codec removes the risk
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


def test_detect_encoding_missing_defaults_to_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brand-new vendor config uses UTF-8 even on a legacy-locale host."""
    import omnigent._encoding as encoding

    monkeypatch.setattr(encoding, "locale_encoding", lambda: "cp1252")
    assert encoding.detect_encoding(tmp_path / "absent.cfg") == "utf-8"


def test_read_existing_cfg_snapshot_missing_is_utf8(tmp_path: Path) -> None:
    """A missing original is the new-config case and has no byte snapshot."""
    import configparser

    from omnigent.cli import _read_existing_cfg_snapshot

    cfg = configparser.ConfigParser()
    encoding, snapshot = _read_existing_cfg_snapshot(cfg, tmp_path / "absent.cfg")
    assert encoding == "utf-8"
    assert snapshot is None
    assert cfg.sections() == []


@pytest.mark.parametrize(
    ("encoding", "expected"),
    [("utf-8", "utf-8"), ("cp1252", "cp1252")],
)
def test_read_existing_cfg_snapshot_loads_one_codec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
    expected: str,
) -> None:
    """The parser and returned snapshot come from the exact same bytes."""
    import configparser

    import omnigent.cli as cli

    original = tmp_path / ".databrickscfg"
    raw = "[legacy]\nhost = https://café.example\n".encode(encoding)
    original.write_bytes(raw)
    monkeypatch.setattr(cli, "locale_encoding", lambda: "cp1252")
    cfg = configparser.ConfigParser()
    detected, snapshot = cli._read_existing_cfg_snapshot(cfg, original)
    assert detected == expected
    assert snapshot == raw
    assert cfg["legacy"]["host"] == "https://café.example"


def test_read_existing_cfg_snapshot_unreadable_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewrite cannot proceed without a byte snapshot of an existing file."""
    import configparser

    from omnigent.cli import _read_existing_cfg_snapshot

    original = tmp_path / ".databrickscfg"
    original.write_text("[legacy]\nhost = https://keep.example\n", encoding="utf-8")
    real_read_bytes = Path.read_bytes

    def _unreadable(path: Path) -> bytes:
        if path == original:
            raise PermissionError("denied")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", _unreadable)
    cfg = configparser.ConfigParser()
    with pytest.raises(PermissionError, match="denied"):
        _read_existing_cfg_snapshot(cfg, original)
    assert cfg.sections() == []


def test_databricks_cfg_merge_lock_excludes_another_thread(tmp_path: Path) -> None:
    """The in-process layer prevents two threads entering the merge together."""
    import threading

    from omnigent.cli import _databricks_cfg_merge_lock

    cfg = tmp_path / ".databrickscfg"
    attempting = threading.Event()
    entered = threading.Event()
    errors: list[BaseException] = []

    def _contender() -> None:
        attempting.set()
        try:
            with _databricks_cfg_merge_lock(cfg):
                entered.set()
        except BaseException as exc:
            errors.append(exc)

    with _databricks_cfg_merge_lock(cfg):
        thread = threading.Thread(target=_contender)
        thread.start()
        assert attempting.wait(timeout=2)
        assert not entered.wait(timeout=0.2)

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert not errors
    assert entered.is_set()
    assert cfg.with_name(f"{cfg.name}.omnigent.lock").exists()


@pytest.mark.windows_only
def test_databricks_cfg_merge_lock_excludes_another_process(tmp_path: Path) -> None:
    """The Windows byte lock serializes independent Omnigent processes."""
    import time

    from omnigent.cli import _databricks_cfg_merge_lock

    cfg = tmp_path / ".databrickscfg"
    ready = tmp_path / "ready"
    entered = tmp_path / "entered"
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from omnigent.cli import _databricks_cfg_merge_lock
        cfg, ready, entered = map(Path, sys.argv[1:])
        ready.write_bytes(b"1")
        with _databricks_cfg_merge_lock(cfg):
            entered.write_bytes(b"1")
        """
    )
    process: subprocess.Popen[str] | None = None
    try:
        with _databricks_cfg_merge_lock(cfg):
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(cfg), str(ready), str(entered)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists()
            assert process.poll() is None
            assert not entered.exists()

        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, f"{stdout}\n{stderr}"
        assert entered.exists()
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def _fake_internal_beta(
    monkeypatch: pytest.MonkeyPatch,
    *profile_names: str,
) -> None:
    """Stub the Databricks-internal module the context manager imports, so the
    end-to-end flow can run in this OSS checkout (which does not ship it)."""
    import sys
    import types

    mod = types.ModuleType("omnigent.onboarding.internal_beta")
    mod.DEFAULT_PROFILES = [  # type: ignore[attr-defined]
        types.SimpleNamespace(name=name) for name in profile_names
    ]
    monkeypatch.setitem(sys.modules, "omnigent.onboarding.internal_beta", mod)


def test_isolated_databricks_cfg_restores_env_on_prep_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If preparation fails after conflicting env vars are popped (here: a failing
    mkstemp), the try/finally must still restore them — not leave DATABRICKS_HOST
    /DATABRICKS_TOKEN deleted.
    """
    import tempfile

    _fake_internal_beta(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("DATABRICKS_HOST", "https://keep.example")

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("mkstemp failed")

    monkeypatch.setattr(tempfile, "mkstemp", _boom)

    from omnigent.cli import _isolated_databricks_cfg

    with pytest.raises(OSError), _isolated_databricks_cfg():
        pass
    assert os.environ.get("DATABRICKS_HOST") == "https://keep.example"


@pytest.mark.windows_only
def test_isolated_databricks_cfg_redetects_codec_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge-back must re-detect the codec. Entry sees UTF-8; another process
    replaces the file with cp1252 mid-flight. Reusing the entry-time UTF-8 codec
    would fail to decode the cp1252 ``é`` (0xE9); re-detecting reads/writes cp1252
    cleanly, so the flow completes and ``café`` survives.
    """
    _fake_internal_beta(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    original = tmp_path / ".databrickscfg"
    original.write_text("[legacy]\nhost = café\n", encoding="utf-8")  # entry: UTF-8

    from omnigent.cli import _isolated_databricks_cfg

    with _isolated_databricks_cfg():
        # Another process rewrites the config as cp1252 mid-flight.
        original.write_bytes("[legacy]\nhost = café\n".encode("cp1252"))

    assert "café" in original.read_text(encoding="cp1252")


def test_isolated_databricks_cfg_preserves_untouched_concurrent_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale seeded section is not overlaid when this setup did not change it."""
    _fake_internal_beta(monkeypatch, "managed")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    original = tmp_path / ".databrickscfg"
    original.write_text("[managed]\ntoken = base\n", encoding="utf-8")
    external = b"[managed]\ntoken = external\n"

    from omnigent.cli import _isolated_databricks_cfg

    with _isolated_databricks_cfg():
        original.write_bytes(external)

    assert b"token = external" in original.read_bytes()


def test_isolated_databricks_cfg_rejects_same_profile_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent edits to the same managed section fail instead of last-wins."""
    _fake_internal_beta(monkeypatch, "managed")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    original = tmp_path / ".databrickscfg"
    original.write_text("[managed]\ntoken = base\n", encoding="utf-8")
    external = b"[managed]\ntoken = external\n"

    from omnigent.cli import _isolated_databricks_cfg

    with pytest.raises(OSError, match="profile 'managed' changed concurrently"):
        with _isolated_databricks_cfg():
            isolated = Path(os.environ["DATABRICKS_CONFIG_FILE"])
            isolated.write_text("[managed]\ntoken = ours\n", encoding="utf-8")
            original.write_bytes(external)

    assert original.read_bytes() == external


def test_isolated_databricks_cfg_fdopen_failure_closes_raw_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``fdopen`` rejects the descriptor, cleanup closes the still-owned raw
    fd before unlinking it (required on Windows) and restores process state.
    """
    import signal
    import tempfile

    _fake_internal_beta(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("DATABRICKS_HOST", "https://keep.example")
    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)
    real_mkstemp = tempfile.mkstemp
    created: dict[str, int | Path] = {}

    def _recording_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        created.update(fd=fd, path=Path(name))
        return fd, name

    def _fdopen_failure(*_args: object, **_kwargs: object) -> None:
        raise OSError("fdopen failed")

    monkeypatch.setattr(tempfile, "mkstemp", _recording_mkstemp)
    monkeypatch.setattr(os, "fdopen", _fdopen_failure)

    from omnigent.cli import _isolated_databricks_cfg

    with pytest.raises(OSError, match="fdopen failed"), _isolated_databricks_cfg():
        pass

    fd = created["fd"]
    assert isinstance(fd, int)
    with pytest.raises(OSError):
        os.fstat(fd)
    path = created["path"]
    assert isinstance(path, Path)
    assert not path.exists()
    assert os.environ.get("DATABRICKS_HOST") == "https://keep.example"
    assert signal.getsignal(signal.SIGINT) is previous_int
    assert signal.getsignal(signal.SIGTERM) is previous_term


def test_isolated_databricks_cfg_close_failure_cannot_mask_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream-close error is attached to, not substituted for, write failure."""
    import configparser

    _fake_internal_beta(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    real_fdopen = os.fdopen

    class _CloseFailure:
        def __init__(self, fd: int, mode: str, encoding: str) -> None:
            self._stream = real_fdopen(fd, mode, encoding=encoding)

        @property
        def closed(self) -> bool:
            return self._stream.closed

        def close(self) -> None:
            self._stream.close()
            raise OSError("close-secondary")

    def _wrapped_fdopen(fd: int, mode: str, *, encoding: str) -> _CloseFailure:
        return _CloseFailure(fd, mode, encoding)

    def _write_failure(self: configparser.ConfigParser, _stream: object) -> None:
        raise ValueError("write-primary")

    monkeypatch.setattr(os, "fdopen", _wrapped_fdopen)
    monkeypatch.setattr(configparser.ConfigParser, "write", _write_failure)

    from omnigent.cli import _isolated_databricks_cfg

    with pytest.raises(ValueError, match="write-primary") as caught:
        with _isolated_databricks_cfg():
            pass
    assert any("close-secondary" in note for note in getattr(caught.value, "__notes__", ()))


def test_isolated_databricks_cfg_merge_fdopen_failure_closes_raw_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The independently created merge stage retains raw-fd ownership on failure."""
    _fake_internal_beta(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    original = tmp_path / ".databrickscfg"
    original_bytes = b"[legacy]\nhost = https://keep.example\n"
    original.write_bytes(original_bytes)
    real_fdopen = os.fdopen
    calls = 0
    failed_fd: int | None = None

    def _fail_second_fdopen(
        fd: int,
        mode: str,
        *,
        encoding: str,
    ) -> object:
        nonlocal calls, failed_fd
        calls += 1
        if calls == 2:
            failed_fd = fd
            raise OSError("merge fdopen failed")
        return real_fdopen(fd, mode, encoding=encoding)

    monkeypatch.setattr(os, "fdopen", _fail_second_fdopen)

    from omnigent.cli import _isolated_databricks_cfg

    with pytest.raises(OSError, match="merge fdopen failed"), _isolated_databricks_cfg():
        pass

    assert failed_fd is not None
    with pytest.raises(OSError):
        os.fstat(failed_fd)
    assert original.read_bytes() == original_bytes
    assert not list(tmp_path.glob(f"{original.name}-merge-*.tmp"))


def test_isolated_databricks_cfg_unlink_failure_does_not_skip_restoration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing temp unlink is reported only after env and signals are restored."""
    import signal

    _fake_internal_beta(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("DATABRICKS_HOST", "https://keep.example")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", "outer.cfg")
    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)
    real_unlink = Path.unlink
    isolated: Path | None = None

    from omnigent.cli import _isolated_databricks_cfg

    try:
        with pytest.raises(PermissionError, match="locked"), _isolated_databricks_cfg():
            isolated = Path(os.environ["DATABRICKS_CONFIG_FILE"])

            def _locked_unlink(path: Path, *args: object, **kwargs: object) -> None:
                if path == isolated:
                    raise PermissionError("locked")
                real_unlink(path, *args, **kwargs)

            monkeypatch.setattr(Path, "unlink", _locked_unlink)

        assert os.environ.get("DATABRICKS_HOST") == "https://keep.example"
        assert os.environ.get("DATABRICKS_CONFIG_FILE") == "outer.cfg"
        assert signal.getsignal(signal.SIGINT) is previous_int
        assert signal.getsignal(signal.SIGTERM) is previous_term
    finally:
        if isolated is not None:
            real_unlink(isolated, missing_ok=True)


def test_isolated_databricks_cfg_returning_signal_handler_keeps_context_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chained handler that returns must not inherit a dismantled context."""
    import signal

    _fake_internal_beta(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("DATABRICKS_HOST", "https://keep.example")
    original_handler = signal.getsignal(signal.SIGTERM)
    calls: list[int] = []

    def _returning_handler(signum: int, _frame: object) -> None:
        calls.append(signum)

    signal.signal(signal.SIGTERM, _returning_handler)
    try:
        from omnigent.cli import _isolated_databricks_cfg

        with _isolated_databricks_cfg():
            isolated = Path(os.environ["DATABRICKS_CONFIG_FILE"])
            active_handler = signal.getsignal(signal.SIGTERM)
            assert callable(active_handler)
            active_handler(signal.SIGTERM, None)
            assert calls == [signal.SIGTERM]
            assert isolated.exists()
            assert os.environ["DATABRICKS_CONFIG_FILE"] == str(isolated)
            assert "DATABRICKS_HOST" not in os.environ
    finally:
        signal.signal(signal.SIGTERM, original_handler)

    assert os.environ.get("DATABRICKS_HOST") == "https://keep.example"


def test_isolated_databricks_cfg_cleanup_cannot_mask_primary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a BaseException during cleanup becomes a note on the body error."""
    _fake_internal_beta(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("DATABRICKS_HOST", "https://keep.example")
    real_unlink = Path.unlink
    isolated: Path | None = None

    from omnigent.cli import _isolated_databricks_cfg

    try:
        with pytest.raises(RuntimeError, match="body failed") as caught:
            with _isolated_databricks_cfg():
                isolated = Path(os.environ["DATABRICKS_CONFIG_FILE"])

                def _interrupted_unlink(path: Path, *args: object, **kwargs: object) -> None:
                    if path == isolated:
                        raise KeyboardInterrupt
                    real_unlink(path, *args, **kwargs)

                monkeypatch.setattr(Path, "unlink", _interrupted_unlink)
                raise RuntimeError("body failed")

        assert any(
            "remove isolated config" in note for note in getattr(caught.value, "__notes__", ())
        )
        assert os.environ.get("DATABRICKS_HOST") == "https://keep.example"
    finally:
        if isolated is not None:
            real_unlink(isolated, missing_ok=True)


def test_isolated_databricks_cfg_missing_owned_temp_aborts_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owned temp is required input; losing it cannot erase real profiles."""
    _fake_internal_beta(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("DATABRICKS_HOST", "https://keep.example")
    original = tmp_path / ".databrickscfg"
    original_bytes = b"[legacy]\nhost = https://keep.example\n"
    original.write_bytes(original_bytes)

    from omnigent.cli import _isolated_databricks_cfg

    with pytest.raises(FileNotFoundError), _isolated_databricks_cfg():
        Path(os.environ["DATABRICKS_CONFIG_FILE"]).unlink()

    assert original.read_bytes() == original_bytes
    assert os.environ.get("DATABRICKS_HOST") == "https://keep.example"


def test_isolated_databricks_cfg_partial_signal_install_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the second signal install fails, the first and all prior mutations undo."""
    import signal

    _fake_internal_beta(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("DATABRICKS_TOKEN", "keep-token")
    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)
    real_signal = signal.signal
    failed = False
    created_path: Path | None = None

    def _fail_first_sigint(signum: int, handler: object) -> object:
        nonlocal failed, created_path
        if signum == signal.SIGINT and not failed:
            failed = True
            created_path = Path(os.environ["DATABRICKS_CONFIG_FILE"])
            raise OSError("signal install failed")
        return real_signal(signum, handler)

    monkeypatch.setattr(signal, "signal", _fail_first_sigint)

    from omnigent.cli import _isolated_databricks_cfg

    with pytest.raises(OSError, match="signal install failed"), _isolated_databricks_cfg():
        pass

    assert created_path is not None
    assert not created_path.exists()
    assert os.environ.get("DATABRICKS_TOKEN") == "keep-token"
    assert signal.getsignal(signal.SIGINT) is previous_int
    assert signal.getsignal(signal.SIGTERM) is previous_term


def test_isolated_databricks_cfg_detects_visible_post_snapshot_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-read change visible to the pre-publish check is preserved."""
    import configparser

    import omnigent.cli as cli

    _fake_internal_beta(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    original = tmp_path / ".databrickscfg"
    original.write_bytes(b"[legacy]\nhost = https://initial.example\n")
    external = b"[external]\nhost = https://new.example\n"
    real_snapshot = cli._read_existing_cfg_snapshot
    calls = 0

    def _race(
        cfg: configparser.ConfigParser,
        path: Path,
    ) -> tuple[str, bytes | None]:
        nonlocal calls
        result = real_snapshot(cfg, path)
        calls += 1
        if calls == 2:
            path.write_bytes(external)
        return result

    monkeypatch.setattr(cli, "_read_existing_cfg_snapshot", _race)

    with pytest.raises(OSError, match="changed while merging"), cli._isolated_databricks_cfg():
        pass

    assert calls == 2
    assert original.read_bytes() == external
    assert not list(tmp_path.glob(f"{original.name}-merge-*.tmp"))


def test_isolated_databricks_cfg_uses_unique_merge_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing legacy stage path cannot be reused or replaced."""
    _fake_internal_beta(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    original = tmp_path / ".databrickscfg"
    original.write_bytes(b"[legacy]\nhost = https://keep.example\n")
    old_shared_stage = original.with_suffix(".tmp")
    sentinel = b"belongs to another process"
    old_shared_stage.write_bytes(sentinel)

    from omnigent.cli import _isolated_databricks_cfg

    with _isolated_databricks_cfg():
        pass

    assert old_shared_stage.read_bytes() == sentinel
    assert not list(tmp_path.glob(f"{original.name}-merge-*.tmp"))
