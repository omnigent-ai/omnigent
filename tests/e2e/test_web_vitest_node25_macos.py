"""Regression tests: the web vitest suite on current Node and on macOS.

Drives the contributor journey of running the web test suite the way
``pnpm test`` in ``web/`` does, under the environments where it used to fail:
a current Node release (25+) and a macOS host.

Two independent facets:

* **Node 25+**: Node exposes an experimental ``localStorage`` key on
  ``globalThis`` unconditionally, and it reads as ``undefined`` unless the
  process was started with ``--localstorage-file``. Because the key already
  exists, vitest's jsdom environment keeps it instead of installing jsdom's
  own ``localStorage``, so every test that touches storage dies with
  ``TypeError: Cannot read properties of undefined (reading 'clear')``.
  :func:`test_storage_tests_survive_node25_undefined_localstorage` recreates
  that exact ``globalThis`` state via a ``--require`` preload (a no-op on
  Node lines where the state already exists natively) and asserts a
  storage-touching test file survives.

* **macOS (any Node)**: jsdom builds its default user agent from
  ``process.platform``, so on a macOS host every test window reports
  ``Mozilla/5.0 (darwin) ... jsdom/<version>`` — which matches none of the
  platform patterns in ``displayNameForHost`` (``web/src/shell/
  NewChatDialog.tsx``), so the landing screen's host chip falls back to the
  host *name* and the two tests asserting "This machine" fail.
  :func:`test_host_chip_tests_pass_under_macos_jsdom_user_agent` pins
  ``navigator.userAgent`` to the exact agent jsdom reports on macOS and
  asserts those two tests pass.

Both tests drive the real vitest suite as a subprocess (the same invocation
``pnpm test`` wraps), so they stay meaningful regression guards after a fix
lands in ``web/src/test-setup.ts`` or in the tests themselves.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"

LOCALSTORAGE_TYPE_ERROR = "Cannot read properties of undefined (reading 'clear')"

# The two landing-screen tests the report names: they assert the host chip
# reads "This machine", which the component derives from navigator.userAgent.
HOST_CHIP_TEST_NAMES = (
    "renders the reference two-layer composer with one in-form control row",
    "keeps compact host and working-directory controls accessibly named",
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _run_vitest(
    args: list[str], extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the web suite's vitest exactly the way ``pnpm test`` does.

    :param args: Arguments appended after ``vitest run``.
    :param extra_env: Environment overrides for the vitest process tree.
    :returns: The completed process and its combined, ANSI-stripped output.
    """
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        pytest.skip("pnpm is not on PATH")
    if not (WEB_DIR / "node_modules").is_dir():
        pytest.skip("web/node_modules is not installed (run `pnpm install` first)")
    env = os.environ.copy()
    # Start from a clean Node invocation so an ambient NODE_OPTIONS can't
    # mask or alter the environment each test constructs deliberately.
    env.pop("NODE_OPTIONS", None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        # --config.verify-deps-before-run=false: read-only run; keeps pnpm
        # from trying to reinstall node_modules when the Node version the
        # suite runs under differs from the one that installed them.
        [pnpm, "--config.verify-deps-before-run=false", "vitest", "run", *args],
        cwd=WEB_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return proc, _strip_ansi(proc.stdout + "\n" + proc.stderr)


def test_storage_tests_survive_node25_undefined_localstorage(tmp_path: Path) -> None:
    """Storage-touching web tests must survive Node 25+'s localStorage global.

    Recreates the ``globalThis`` state Node 25+ ships by default — a
    ``localStorage`` key that exists but reads ``undefined`` — and runs a
    storage-touching test file. Pre-fix, vitest's jsdom environment keeps the
    Node-provided key, so every ``localStorage.clear()`` in the file's hooks
    throws the reported TypeError and the file fails wholesale.
    """
    shim = tmp_path / "node25_localstorage_globalthis.cjs"
    shim.write_text(
        textwrap.dedent(
            """\
            // Node 25+ unconditionally exposes an experimental localStorage key
            // on globalThis that reads as undefined unless --localstorage-file
            // is provided. Recreate that exact state on older Node lines (a
            // no-op where the key already exists natively).
            if (!("localStorage" in globalThis)) {
              let value;
              Object.defineProperty(globalThis, "localStorage", {
                configurable: true,
                enumerable: false,
                get() {
                  return value;
                },
                set(v) {
                  value = v;
                },
              });
            }
            """
        )
    )
    proc, output = _run_vitest(
        ["src/shell/Sidebar.shiftSelect.test.tsx"],
        extra_env={"NODE_OPTIONS": f"--require {shim}"},
    )
    assert LOCALSTORAGE_TYPE_ERROR not in output, (
        "web vitest tests crash when Node ships localStorage as an undefined "
        f"global (Node 25+ default): {LOCALSTORAGE_TYPE_ERROR!r} in output.\n"
        f"{_tail(output)}"
    )
    assert proc.returncode == 0, (
        "vitest exited non-zero with Node 25+'s undefined localStorage "
        f"global (exit {proc.returncode}).\n{_tail(output)}"
    )


def test_host_chip_tests_pass_under_macos_jsdom_user_agent() -> None:
    """The landing host-chip tests must pass under macOS's jsdom user agent.

    jsdom derives its default user agent from ``process.platform``, so on a
    macOS checkout it reports ``Mozilla/5.0 (darwin) ...``. Pre-fix that
    string matches none of ``displayNameForHost``'s platform patterns, the
    chip falls back to the host name, and the two tests asserting
    "This machine" fail — on any Node version.
    """
    token = uuid.uuid4().hex[:8]
    # vitest resolves setupFiles and the base config relative to the config
    # file's directory, so both generated files must live inside web/.
    setup = WEB_DIR / f"vitest.node25-macos-{token}.setup.ts"
    config = WEB_DIR / f"vitest.node25-macos-{token}.config.mts"
    setup.write_text(
        textwrap.dedent(
            """\
            // jsdom computes its default user agent from process.platform, so
            // on a macOS host every test window reports the "(darwin)" agent
            // below. Pinning it reproduces the macOS jsdom environment on any
            // OS.
            import { createRequire } from "node:module";

            const jsdomVersion = createRequire(import.meta.url)(
              "jsdom/package.json",
            ).version;

            Object.defineProperty(navigator, "userAgent", {
              value:
                "Mozilla/5.0 (darwin) AppleWebKit/537.36 " +
                `(KHTML, like Gecko) jsdom/${jsdomVersion}`,
              configurable: true,
            });
            """
        )
    )
    config.write_text(
        textwrap.dedent(
            f"""\
            import {{ mergeConfig }} from "vitest/config";
            import base from "./vite.config";

            // The web suite's real config plus one extra setup file that pins
            // navigator.userAgent to what jsdom reports on a macOS host.
            export default mergeConfig(base, {{
              test: {{
                setupFiles: ["./{setup.name}"],
              }},
            }});
            """
        )
    )
    try:
        proc, output = _run_vitest(
            [
                "--config",
                config.name,
                "src/shell/NewChatDialog.test.tsx",
                "-t",
                "|".join(re.escape(name) for name in HOST_CHIP_TEST_NAMES),
            ]
        )
        assert re.search(r"\b2 passed\b", output), (
            "the two NewChatDialog host-chip tests did not both pass under "
            "the macOS jsdom user agent (the chip shows the host-name "
            f"fallback instead of 'This machine').\n{_tail(output)}"
        )
        assert proc.returncode == 0, (
            "vitest exited non-zero running the host-chip tests under the "
            f"macOS jsdom user agent (exit {proc.returncode}).\n{_tail(output)}"
        )
    finally:
        setup.unlink(missing_ok=True)
        config.unlink(missing_ok=True)
