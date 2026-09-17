"""E2E regression: host model-options tests must ignore ambient provider config.

Reproduces the developer journey from the bug report "tests/host/test_connect.py
model-options tests fail on machines whose ambient pi default is a subscription":

1. the machine's ambient omnigent config sets subscription provider defaults —
   the pi surface pinned to Pi's own CLI login (``default: pi`` on a
   ``kind: subscription, cli: pi`` entry), with claude/codex subscription
   logins defaulted alongside (the keyring-login-rich developer machine),
2. the developer runs the host model-options tests on an unpatched tree::

       pytest tests/host/test_connect.py -k "handle_model_options or model_options_frame"

3. the host resolves the ambient provider while answering model-options frames
   and decorates every model row with
   ``source={'kind': 'subscription', 'label': 'Subscription', 'name': ...}``,
   so the suite's exact-frame assertions fail even though the tree is fine —
   local runs look broken, and unrelated patches get blamed.

This test drives that journey in a nested pytest whose config home carries the
ambient subscription defaults, and requires the nested run to be green: the
model-options tests must isolate the source-resolution seam (or otherwise stop
absorbing the machine's ambient config). ``$OMNIGENT_CONFIG_HOME`` is the
onboarding layer's own config-home relocation, so the nested run resolves the
staged config through exactly the code path a real ``~/.omnigent/config.yaml``
takes.

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
    pi/anthropic/openai surfaces, then runs the real developer command against
    it. Without seam isolation this FAILS: the frames gain
    ``source={'kind': 'subscription', ...}`` on every model row and the
    exact-frame assertions report failures on a pristine tree.
    """
    config_home = tmp_path / "ambient-omnigent-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(_AMBIENT_SUBSCRIPTION_CONFIG, encoding="utf-8")

    env = os.environ.copy()
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)

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
        "host model-options tests absorbed the machine's ambient provider config "
        "(subscription source decoration leaked into the model-options frames) "
        f"instead of isolating it — nested pytest exited {nested.returncode}:\n"
        f"{output}"
    )
