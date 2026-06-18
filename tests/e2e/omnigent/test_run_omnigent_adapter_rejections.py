"""
End-to-end: omnigent example YAMLs that declare unsupported
concepts MUST fail loud at spec-load time under Omnigent mode.

The adapter in :mod:`omnigent.spec.omnigent` rejects several
concepts it cannot faithfully translate into an omnigent
:class:`AgentSpec` (see
:func:`_reject_unsupported_concepts_def`):

- ``policies`` — label-based + function-type policies have no
  omnigent parity yet.
- MCP-type tools — omnigent' subprocess MCP transport has no
  omnigent equivalent (omnigent uses HTTP/SSE MCP only).

Silent translation of these would give the user an agent that
LOOKS configured (no error at boot) but lacks the policies /
tools the YAML promised — a foot-gun. The right behavior is
``omnigent run <yaml>`` exits non-zero with an error
message naming the specific field.

This test parametrizes over every example YAML that trips at
least one rejection, and asserts:

1. Exit code is non-zero.
2. stderr contains the expected field name (``"guardrails.policies"``
   or ``"mcp_servers"``).

**What breaks if this test fails:**

- :func:`_reject_unsupported_concepts_def` silently drops one
  of these concepts — a YAML author declares a policy, gets an
  unpoliced agent, security-relevant behavior is silently
  missing.
- The CLI's error-propagation path swallows the adapter's
  :class:`OmnigentError` and exits 0.
- Someone adds translation support for one of these concepts
  but forgets to remove its entry here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests._model_pools import resolve_model

_MODEL = resolve_model("databricks-gpt-5-mini", key=__name__)
_HARNESS = "openai-agents"
_TIMEOUT_SEC = 30


# (yaml_relpath, expected_error_substring, id)
#
# The rejection surface in :func:`_reject_unsupported_concepts`
# has shrunk over time as the Omnigent translator has grown:
#
# - **Policies**: lifted into ``AgentSpec.guardrails.policies``
#   and enforced by the workflow layer (see
#   ``test_run_omnigent_policy_enforcement.py``). No longer rejected.
# - **MCP servers / tools**: the stdio MCP transport landed in
#   commit a081406 ("stdio MCP: finish the round-trip");
#   :class:`MCPServerConfig` round-trips through the reverse
#   translator as :class:`MCPTool`. No longer rejected.
# - **`tools.sandbox.container_image`**: still rejected, but no
#   example YAML in the repo declares it, so there's nothing to
#   parametrize over today. The drift-guard intent (a yaml
#   author silently loses sandbox isolation) survives in the
#   adapter's :func:`_reject_unsupported_concepts` and its unit
#   tests; this e2e file is a no-op until a YAML lands that
#   exercises the surviving rejection path.
#
# Empty parametrize → pytest collects zero test instances. The
# scaffolding stays so a future case (when a new "still
# unsupported" concept lands) can be added without re-deriving
# the subprocess invocation pattern.
_REJECTION_CASES: list[pytest.param] = []


@pytest.mark.parametrize("yaml_rel,expected_error", _REJECTION_CASES)
def test_run_omnigent_rejects_unsupported_yaml(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    omnigent_credentials_env: dict[str, str],
    databricks_workspace: tuple[str, str],
    yaml_rel: str,
    expected_error: str,
) -> None:
    """
    ``omnigent run <yaml> -p ...`` exits non-zero and
    mentions *expected_error* when the YAML trips a spec-load
    rejection.

    The one-shot ``-p`` form is used so the subprocess exits
    immediately after the rejection — no need to drive a REPL
    to hit the adapter. We don't pass a real LLM prompt because
    the rejection fires before any LLM request.

    :param omnigent_python: Shared interpreter fixture.
    :param omnigent_repo_root: Subprocess cwd.
    :param omnigent_credentials_env: Env with PAT + profile.
    :param yaml_rel: Path under *omnigent_repo_root* to the
        example YAML to load.
    :param expected_error: Substring the adapter's
        :class:`OmnigentError` message MUST contain — the
        specific field name that tripped the rejection.
    """
    yaml_path = omnigent_repo_root / yaml_rel
    assert yaml_path.exists(), f"Fixture YAML missing: {yaml_path}"

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "-p",
            # Arbitrary prompt — the adapter rejects before any
            # LLM roundtrip so the text doesn't matter.
            "hello",
        ],
        env=omnigent_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SEC,
    )

    # Non-zero exit proves the adapter's error propagated.
    # Silent success (exit 0) would mean the concept was
    # translated incorrectly — exactly the foot-gun this test
    # exists to catch.
    assert result.returncode != 0, (
        f"--omnigent on {yaml_rel} exited 0 but should have rejected the "
        f"spec at load time. stderr={result.stderr[-1500:]!r}"
    )
    # stderr must name the specific field so the YAML author
    # can fix their spec. A generic "invalid" / "error" would
    # be an unhelpful regression.
    combined = result.stdout + result.stderr
    assert expected_error in combined, (
        f"Expected error substring {expected_error!r} missing from "
        f"--omnigent rejection output. The adapter may have raised a less-"
        f"specific error than the OmnigentError in "
        f"_reject_unsupported_concepts_def. "
        f"stderr tail:\n{result.stderr[-1500:]}"
    )
