"""Import-cost guards for the command hook modules of native harnesses.

The registered per-tool hooks are answered by the runner relay over curl, so
these modules run as the relay-less fallback and for the other hook events
(prompt submit, session lifecycle, turn routing). Claude Code and Codex still
block on those spawns, so the guards pin each entrypoint's fresh-interpreter
import graph (the deterministic form of the per-spawn latency claim).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import omnigent

_REPO_ROOT = Path(omnigent.__file__).resolve().parent.parent
_HOOK_MODULES = (
    "omnigent.harnesses.claude_native.hook",
    "omnigent.harnesses.codex_native.hook",
)
# Third-party graphs that cost hundreds of milliseconds to import and that a
# hook only needs on the paths that actually speak HTTP or render output.
_HEAVY_IMPORTS = ("fastapi", "httpx", "pydantic", "rich")


def _loaded_after(statements: str, stdin: str = "") -> list[str]:
    """Run *statements* in a fresh ``python -I`` and list loaded heavy/bridge modules."""
    code = "\n".join(
        (
            "import json, sys",
            f"sys.path.insert(0, {str(_REPO_ROOT)!r})",
            statements,
            f"heavy = [m for m in {list(_HEAVY_IMPORTS)!r} if m in sys.modules]",
            "bridges = [m for m in sys.modules if m.endswith('_native.bridge')]",
            "print(json.dumps(sorted(heavy + bridges)))",
        )
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("module", _HOOK_MODULES)
def test_hook_module_import_loads_no_heavy_third_party_graph(module: str) -> None:
    loaded = [m for m in _loaded_after(f"import {module}") if m in _HEAVY_IMPORTS]
    assert loaded == [], f"{module} imports {loaded} on every hook spawn"


@pytest.mark.parametrize("module", _HOOK_MODULES)
def test_observe_tool_path_skips_the_harness_bridge(module: str, tmp_path: Path) -> None:
    # With no relay advertised in the bridge dir, the observer runs up to its
    # failed relay lookup and exits 0, so only its imports are measured.
    statements = "\n".join(
        (
            f"from {module} import main",
            f"assert main(['observe-tool', '--bridge-dir', {str(tmp_path)!r}]) == 0",
        )
    )
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {}}
    loaded = _loaded_after(statements, stdin=json.dumps(payload))
    assert loaded == [], f"observe-tool via {module} loads {loaded} before dispatching"
