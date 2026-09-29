"""
Bundled-agent launch under a non-UTF-8 locale.

Reproduces the reported crash: on a machine whose preferred encoding is not
UTF-8 (the reporter's Chinese Windows 10 install defaults to cp936/GBK),
every bundled agent dies at launch::

    $ omnigent debby -p "hello"
    UnicodeDecodeError: 'gbk' codec can't decode byte 0x94 in position 10

``_bundled_agent_brain_harness`` (``omnigent/cli.py``) reads the bundled
agent's ``config.yaml`` with ``Path.read_text()`` and no ``encoding=``, so
the read uses ``locale.getpreferredencoding()``. The file begins with a
UTF-8 em-dash (``# Debby — ...``), and the ``except (OSError,
yaml.YAMLError)`` guard around the read does not cover
``UnicodeDecodeError`` (a ``ValueError`` subclass), so the decode error
escapes the handler and crashes the CLI before any credential or provider
is consulted.

This test drives the real user journey: it spawns ``python -m omnigent
<agent> -p "hello"`` under a pseudo-TTY with the child forced onto a
non-UTF-8 preferred encoding — ``LC_ALL=C`` with PEP 538 locale coercion
and PEP 540 UTF-8 mode explicitly off, which resolves to ASCII on Linux,
the same locale-dependent ``read_text()`` default that resolves to GBK on
the reporter's box (ASCII dies on the em-dash's first byte ``0xe2``; GBK
on its third byte ``0x94``). The test fails when the CLI dies with
``UnicodeDecodeError``. Fixed behavior — the CLI getting past the
bundled-config read into the normal launch path (or exiting for any
non-decode reason, e.g. no credential configured) — passes.

Usage::

    python -m pytest tests/e2e/test_bundled_agent_nonutf8_locale_e2e.py -v --timeout=180
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Ceiling for observing the crash. The buggy read happens right after CLI
# import/dispatch (a few seconds in); a child still alive with no
# UnicodeDecodeError after this long is well past the bundled-config read
# and into the launch path proper — which is the fixed behavior.
_CRASH_WINDOW_S = 60


def _non_utf8_env(home: Path) -> dict[str, str]:
    """Child env that forces a non-UTF-8 preferred encoding.

    Mirrors the reporter's environment (a cp936/GBK Windows console) with
    the portable equivalent: the POSIX locale (ASCII preferred encoding),
    with PEP 538 C-locale coercion and PEP 540 UTF-8 mode explicitly
    disabled so Python does not silently rescue the locale. HOME points at
    a fresh directory so no local omnigent config alters the journey, and
    ambient OMNIGENT_* runner/host vars are stripped so the child behaves
    like a user's own shell.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("OMNIGENT_")}
    env.pop("RUNNER_SERVER_URL", None)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["PYTHONUTF8"] = "0"
    env["PYTHONCOERCECLOCALE"] = "0"
    # Resolve `-m omnigent` to this worktree, not a shadowing install.
    env["PYTHONPATH"] = str(_REPO_ROOT)
    return env


def _preferred_encoding(env: dict[str, str]) -> str:
    """Preferred text-encoding the child interpreter will actually use."""
    probe = subprocess.run(
        [sys.executable, "-c", "import locale; print(locale.getpreferredencoding(False))"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return probe.stdout.strip()


@pytest.mark.parametrize("agent", ["debby", "polly"])
def test_bundled_agent_launch_survives_non_utf8_locale(agent: str, tmp_path: Path) -> None:
    """``omnigent debby|polly -p "hello"`` must not die decoding its own config."""
    home = tmp_path / "home"
    home.mkdir()
    env = _non_utf8_env(home)

    encoding = _preferred_encoding(env)
    if "utf" in encoding.replace("-", "").lower():
        pytest.skip(
            f"interpreter forces UTF-8 preferred encoding ({encoding!r}); "
            "this platform cannot exhibit a locale-dependent read_text()"
        )

    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", agent, "-p", "hello"],
        cwd=str(tmp_path),
        env=env,
        encoding="utf-8",
        codec_errors="replace",
        timeout=_CRASH_WINDOW_S,
    )
    try:
        idx = child.expect(
            ["UnicodeDecodeError", pexpect.EOF, pexpect.TIMEOUT],
            timeout=_CRASH_WINDOW_S,
        )
        if idx == 0:
            # Drain briefly so the failure message carries the codec line
            # ("'ascii' codec can't decode byte 0xe2 ..." / "'gbk' ... 0x94").
            with contextlib.suppress(pexpect.exceptions.ExceptionPexpect):
                child.expect([pexpect.EOF, pexpect.TIMEOUT], timeout=15)
            detail = (
                (child.before or "") + (child.after if isinstance(child.after, str) else "")
            )[:2000]
            pytest.fail(
                f'`omnigent {agent} -p "hello"` crashed with UnicodeDecodeError under a '
                f"non-UTF-8 locale (preferred encoding {encoding!r}): the bundled "
                f"config.yaml is read with locale-dependent Path.read_text() and the "
                f"decode error is not swallowed. Crash detail: UnicodeDecodeError{detail}"
            )
        if idx == 1:
            # The CLI exited without a decode crash (e.g. no credential
            # configured) — the bundled-config read survived, which is all
            # this regression guards.
            output = child.before or ""
            assert "UnicodeDecodeError" not in output
        # idx == 2 (TIMEOUT): still running well past the crash window —
        # the launch proceeded beyond the bundled-config read. Pass.
    finally:
        with contextlib.suppress(Exception):
            child.close(force=True)
