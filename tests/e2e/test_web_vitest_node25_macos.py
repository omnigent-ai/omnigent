"""Guards for running the web vitest suite on current Node (25+) and on macOS."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

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
    """Run the web suite's vitest as ``pnpm test`` does, loading ``preload`` first.

    Returns the exit code and the ANSI-stripped combined output.
    """
    if not _VITEST.exists():
        pytest.skip("web toolchain not installed (run pnpm install first)")
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")
    env = os.environ.copy()
    # An ambient NODE_OPTIONS or VITEST must not mask the environment each guard builds.
    env.pop("NODE_OPTIONS", None)
    env.pop("VITEST", None)
    env["NODE_OPTIONS"] = f'--require "{preload}"'
    result = subprocess.run(
        [str(_VITEST), "run", *files],
        cwd=_WEB_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
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
