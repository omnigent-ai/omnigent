"""E2E regression: host model-options tests must ignore ambient provider state.

Reproduces the developer journey from the bug report "tests/host/test_connect.py
model-options tests fail on machines whose ambient pi default is a subscription":

1. the machine carries ambient provider state — an omnigent config whose pi
   surface is pinned to Pi's own CLI login (``default: pi`` on a
   ``kind: subscription, cli: pi`` entry), claude/codex subscription logins
   defaulted alongside, and vendor API keys exported in the environment (the
   credential-rich developer machine),
2. the developer runs the host model-options tests on an unpatched tree::

       pytest tests/host/test_connect.py -k "handle_model_options or model_options_frame"

3. the host resolves the ambient provider while answering model-options frames
   and decorates every model row with
   ``source={'kind': 'subscription', 'label': 'Subscription', 'name': ...}``,
   so the suite's exact-frame assertions fail even though the tree is fine —
   local runs look broken, and unrelated patches get blamed.

This test drives that journey in a nested pytest whose config home carries the
ambient subscription defaults and whose environment carries vendor API keys,
and requires the nested run to be green: the model-options tests must isolate
the whole source-resolution seam — both ``load_config()`` (relocated by
``$OMNIGENT_CONFIG_HOME``, the onboarding layer's own config-home seam, so the
staged config takes exactly the code path a real ``~/.omnigent/config.yaml``
takes) and ambient-credential detection (which reads vendor env keys).

No LLM and no live server are needed — this is pure host-side resolution — so
it runs without ``--llm-api-key``::

    .venv/bin/python -m pytest tests/e2e/test_host_model_options_ambient_isolation_e2e.py -v
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The ambient machine state from the bug report: the pi surface defaulted to a
# subscription (the documented entry), plus claude/codex subscription defaults
# for the anthropic/openai surfaces (the reporter's keyring-backed CLI logins,
# which is what breaks the claude-native / codex-native / claude-sdk siblings).
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

# Vendor keys ambient-credential detection would adopt as provider defaults
# when the config declares none (obviously fake values).
_AMBIENT_DETECTION_ENV = {
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "OPENAI_API_KEY": "test-openai-key",
}

# The exact selection from the bug report's journey. Every model-options test
# rides through HostProcess._handle_model_options, the seam that resolves the
# ambient provider config.
_MODEL_OPTIONS_SELECTION = "handle_model_options or model_options_frame"

# Budget for the nested pytest run: a handful of tests, but a cold interpreter
# importing the host graph first.
_NESTED_PYTEST_TIMEOUT_S = 240.0


def test_model_options_tests_ignore_ambient_subscription_defaults(tmp_path: Path) -> None:
    """The host model-options tests must pass on a subscription-default machine.

    Stages a config home whose providers resolve subscription defaults for the
    pi/anthropic/openai surfaces plus detectable vendor env keys, then runs the
    real developer command against them. Without seam isolation this FAILS: the
    frames gain ``source={'kind': 'subscription', ...}`` on every model row and
    the exact-frame assertions report failures on a pristine tree.
    """
    config_home = tmp_path / "ambient-omnigent-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(_AMBIENT_SUBSCRIPTION_CONFIG, encoding="utf-8")

    # Drop the outer runner's pytest vars so the nested run starts clean, then
    # stage the ambient machine state.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    env.update(_AMBIENT_DETECTION_ENV)

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
            "-p",
            "no:randomly",
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=_NESTED_PYTEST_TIMEOUT_S,
        check=False,
    )

    output = f"{nested.stdout}\n{nested.stderr}"
    # Guard against a vacuous pass: renamed/deselected tests exit with pytest's
    # no-tests-collected code (5), which must read as a broken guard, not green.
    passed = re.search(r"(\d+) passed", output)
    assert nested.returncode == 0 and passed and int(passed.group(1)) > 0, (
        "host model-options tests absorbed the machine's ambient provider state "
        "(subscription/key source decoration leaked into the model-options frames) "
        f"instead of isolating it — nested pytest exited {nested.returncode}:\n"
        f"{output}"
    )
