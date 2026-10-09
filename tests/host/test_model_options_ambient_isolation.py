"""Model-options frame assertions must not depend on machine-global provider state.

Runs the developer's command in a child pytest seeded with every ambient provider
channel (config home, inference overlay, vendor API keys); host-side only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Ambient state under test: pi defaults to a subscription, with claude/codex
# subscription defaults covering the anthropic/openai surfaces.
_AMBIENT_SUBSCRIPTION_CONFIG = """\
providers:
  pi:
    kind: subscription
    cli: pi
    default: pi
  claude:
    kind: subscription
    cli: claude
    default: true
  codex:
    kind: subscription
    cli: codex
    default: true
"""

# The managed-sandbox overlay replaces the local providers block wholesale.
_AMBIENT_INFERENCE_OVERLAY = {
    "providers": {"claude": {"kind": "subscription", "cli": "claude", "default": True}}
}

# Vendor keys that ambient detection adopts as provider defaults (fake values).
_AMBIENT_DETECTION_ENV = {
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "OPENAI_API_KEY": "test-openai-key",
}

# Every selected test answers a frame through HostProcess._handle_model_options.
_MODEL_OPTIONS_SELECTION = "handle_model_options or model_options_frame"

# Stays under the outer E2E job's 180s per-test cap; a cold run takes seconds.
_NESTED_PYTEST_TIMEOUT_S = 150.0


def _partial_output(exc: subprocess.TimeoutExpired) -> str:
    return "\n".join(
        part.decode(errors="replace") if isinstance(part, bytes) else (part or "")
        for part in (exc.stdout, exc.stderr)
    )


def test_model_options_tests_ignore_ambient_subscription_defaults(tmp_path: Path) -> None:
    """The host model-options tests pass despite hostile ambient provider state."""
    config_home = tmp_path / "ambient-omnigent-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(_AMBIENT_SUBSCRIPTION_CONFIG, encoding="utf-8")
    overlay = tmp_path / "inference-overlay.json"
    overlay.write_text(json.dumps(_AMBIENT_INFERENCE_OVERLAY), encoding="utf-8")

    # Drop the outer runner's pytest vars so the nested run starts clean.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    env["OMNIGENT_INFERENCE_CONFIG"] = str(overlay)
    env.update(_AMBIENT_DETECTION_ENV)

    timed_out: str | None = None
    try:
        nested = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/host/test_connect.py",
                "-k",
                _MODEL_OPTIONS_SELECTION,
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=_NESTED_PYTEST_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = _partial_output(exc)
    finally:
        # The inherited environment may carry credentials; keep it out of --showlocals.
        env.clear()
    if timed_out is not None:
        pytest.fail(
            f"nested pytest exceeded {_NESTED_PYTEST_TIMEOUT_S:.0f}s; partial output:\n{timed_out}"
        )

    output = f"{nested.stdout}\n{nested.stderr}"
    # Exit 5 (nothing collected) must read as a broken guard, not as green.
    passed = re.search(r"(\d+) passed", output)
    assert nested.returncode == 0 and passed and int(passed.group(1)) > 0, (
        "host model-options tests absorbed the machine's ambient provider state "
        f"instead of isolating it — nested pytest exited {nested.returncode}:\n{output}"
    )
