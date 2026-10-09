"""Guards for running the web vitest suite on current Node (25+) and on macOS."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# Keep pytest's timeout above the 900 s subprocess timeout so a hung vitest run
# fails with its captured output instead of pytest killing the worker first.
pytestmark = pytest.mark.timeout(960)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WEB_DIR = _REPO_ROOT / "web"
_VITEST = _WEB_DIR / "node_modules" / ".bin" / "vitest"
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# The reported failure: storage calls on Node's undefined localStorage global.
_STORAGE_TYPE_ERROR = re.compile(
    r"Cannot read properties of undefined \(reading '(?:clear|getItem|setItem|removeItem)'\)"
)

# The globals Node 25+ ships by default, defined before any user code runs.
_NODE25_GLOBALS_SHIM = """\
class NodeStorage {
  #values = new Map();
  get length() { return this.#values.size; }
  clear() { this.#values.clear(); }
  getItem(key) { return this.#values.get(String(key)) ?? null; }
  key(index) { return [...this.#values.keys()][index] ?? null; }
  removeItem(key) { this.#values.delete(String(key)); }
  setItem(key, value) { this.#values.set(String(key), String(value)); }
}
function defineReplaceable(name, initial) {
  let value = initial;
  Object.defineProperty(globalThis, name, {
    configurable: true,
    enumerable: name !== "localStorage",
    get() { return value; },
    set(next) { value = next; },
  });
}
defineReplaceable("localStorage", undefined);
defineReplaceable("sessionStorage", new NodeStorage());
Object.defineProperty(globalThis, "Storage", {
  configurable: true, writable: true, enumerable: false, value: NodeStorage,
});"""

# jsdom derives its default navigator.userAgent from process.platform, so a
# worker that reads "darwin" gets the agent a Mac reports. Only vitest workers
# (spawned with VITEST=true) change: the CLI picks native bindings by platform.
_DARWIN_PLATFORM_PRELOAD = """\
if (process.env.VITEST === "true") {
  Object.defineProperty(process, "platform", {
    value: "darwin",
    configurable: true,
    enumerable: true,
    writable: false,
  });
}"""

_STORAGE_TOUCHING_FILES = (
    "src/test-setup.storage.test.ts",
    "src/extensions/services/storage.test.ts",
    "src/lib/themePalette.test.ts",
    "src/lib/chunkLoadRecovery.test.ts",
    "src/lib/terminalClipboardPreferences.test.ts",
    "src/components/ChunkLoadErrorBoundary.test.tsx",
)

# Only the landing screen derives its host-chip label from navigator.userAgent.
_USER_AGENT_SENSITIVE_FILES = ("src/shell/NewChatDialog.test.tsx",)


def _run_vitest(files: tuple[str, ...], preload: Path) -> tuple[int, str]:
    """Run vitest as ``pnpm test`` does, with ``preload`` required first."""
    if not _VITEST.exists():
        pytest.skip("web toolchain not installed (run pnpm install first)")
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")
    # vitest treats paths as filters, so a renamed file would silently leave the guard.
    missing = [file for file in files if not (_WEB_DIR / file).exists()]
    assert not missing, f"guarded web test files were moved or removed: {missing}"
    env = os.environ.copy()
    # Replace ambient NODE_OPTIONS and VITEST so each guard controls the environment it builds.
    env.pop("VITEST", None)
    env["NODE_OPTIONS"] = f'--require "{preload}"'
    try:
        result = subprocess.run(
            [str(_VITEST), "run", *files],
            cwd=_WEB_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        output = _ANSI.sub("", f"{exc.stdout or ''}\n{exc.stderr or ''}")
        pytest.fail(f"vitest did not finish within {exc.timeout}s:\n{_tail(output)}")
    return result.returncode, _ANSI.sub("", f"{result.stdout}\n{result.stderr}")


def _tail(output: str, lines: int = 40) -> str:
    return "\n".join(output.splitlines()[-lines:])


def test_storage_tests_survive_node25_globals(tmp_path: Path) -> None:
    """Storage-touching web tests pass when Node predefines the storage globals."""
    shim = tmp_path / "node25-globals.cjs"
    shim.write_text(_NODE25_GLOBALS_SHIM, encoding="utf-8")
    returncode, output = _run_vitest(_STORAGE_TOUCHING_FILES, shim)
    reason = " with the storage TypeError" if _STORAGE_TYPE_ERROR.search(output) else ""
    assert returncode == 0, (
        f"vitest exited {returncode} under Node 25+ globals{reason}:\n{_tail(output)}"
    )


def test_host_chip_tests_pass_under_macos_user_agent(tmp_path: Path) -> None:
    """User-agent-sensitive web tests pass under jsdom's macOS agent."""
    preload = tmp_path / "darwin-platform.cjs"
    preload.write_text(_DARWIN_PLATFORM_PRELOAD, encoding="utf-8")
    returncode, output = _run_vitest(_USER_AGENT_SENSITIVE_FILES, preload)
    assert returncode == 0, f"vitest exited {returncode} under the macOS agent:\n{_tail(output)}"
