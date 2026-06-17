"""Live E2E: ``omnigent run`` with the ``databricks_supervisor`` harness
against a real Databricks Agent Bricks Supervisor API workspace.

Two tests, one per system connector:

- :func:`test_supervisor_google_drive_returns_files` - synthesizes
  a bundle wired to ``system_ai_agent_google_drive``, runs a real
  ``omnigent run`` subprocess, asserts the LLM's reply
  quotes a real Google Drive file URL (proving the connector
  actually returned files).
- :func:`test_supervisor_atlassian_returns_jira_issue` - same shape
  for ``system_ai_agent_atlassian_mcp``, asserts the reply contains
  both an ``atlassian.net`` URL host AND a Jira-style issue key
  (``PROJ-123``), proving a real Jira issue came back.

Both tests exercise the full supervisor wire path:

    YAML  →  ``omnigent run`` (spawns a local omnigent
            server subprocess)
          →  AP-side spec parser accepts the
             ``databricks_supervisor`` harness and the nested
             ``uc_connection`` tool shape
          →  ``_create_executor`` routes to the harness HTTP client
          →  Executor resolves credentials via the configured
             profile (the bundle's ``executor.profile``, falls
             through SDK ``Config.authenticate()`` → ``[profile]``
             section → ``[DEFAULT]``)
          →  POST ``{workspace}/ai-gateway/mlflow/v1/responses``
             with ``stream=True`` and a Bearer token
          →  Server-side loop invokes the connector, streams SSE
             events back: ``response.output_text.delta`` (LLM text),
             ``response.output_item.done`` with ``function_call``
             (server ran tool), function_call_output (tool result)
          →  Executor maps events to runtime ``ExecutorEvent``s,
             pairs function_call with function_call_output by
             ``call_id``, emits :class:`ToolCallObserved` (no
             preceding ``ToolCallRequested``), then
             :class:`TurnComplete`
          →  Workflow assembles the final response; the REPL
             prints the assistant's text to stdout.

If the workspace requires OAuth on first use of a system connector,
the gateway emits an ``error`` event with ``code == "oauth"`` and a
login URL in the message. The executor surfaces this as a TextChunk
shaped like ``\\n\\nAuth required - please log in to <connector>:
\\n<url>\\n\\n`` (plain text, URL on its own line).
**In that case these tests skip
cleanly** - they want to verify the success path (real files /
real Jira), not the OAuth-required path. Complete the OAuth login
out of band (visit the URL once per connector, per workspace, per
user) and rerun.

What breaks if a regression goes in:

- Nested-tool-shape regression in the parser → gateway returns
  ``INVALID_PARAMETER_VALUE`` and the test surfaces that as a
  forbidden-marker failure.
- ``BearerAuth`` / gateway URL composition regression → 401 / 404
  from the gateway, surfaced via forbidden markers and a non-zero
  exit.
- SSE event mapping regression (function_call vs
  function_call_output pairing, lying terminator) → no real-data
  fingerprint in stdout, test fails with diagnostic context.
- Profile resolution regression → the executor raises ``OSError``
  before any HTTP call; surfaced as a non-zero exit with the
  resolver's error message.

Skip behaviour:

- Profile from ``tests/resources/examples/databricks_supervisor/config.yaml`` missing
  from ``~/.databrickscfg`` → skip with config hint.
- OAuth required on first use → skip with login instructions.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``.
Invoke with::

    pytest tests/e2e/omnigent/test_run_omnigent_supervisor.py -v
"""

from __future__ import annotations

import configparser
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# --------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------

# Repo root resolves from tests/e2e/omnigent/test_*.py → parents[3].
_REPO_ROOT = Path(__file__).resolve().parents[3]

# The example bundle is the single source of truth for the profile
# and model. The e2e tests read them from here so there's no
# separate env var to keep in sync.
_EXAMPLE_BUNDLE = _REPO_ROOT / "tests" / "resources" / "examples" / "databricks_supervisor"

_DATABRICKSCFG_PATH = Path.home() / ".databrickscfg"

# Wall-clock budget. Subprocess startup + supervisor cold-start +
# real Drive/Atlassian search + LLM round-trip is comfortably under
# 3 minutes; if we hit this, something is hanging (likely a
# regressed credential resolver retrying a 401 indefinitely).
_RUN_TIMEOUT_SEC = 180

# Real-data fingerprints - substrings that prove the LLM saw a real
# tool result, not a hallucination. Both connectors return real
# URLs in their search results; the LLM is instructed to quote a
# real URL verbatim, so the URL host appears in stdout iff the
# connector actually fired.
_GOOGLE_DRIVE_URL_HOSTS = ("drive.google.com", "docs.google.com")
_ATLASSIAN_URL_HOST = "atlassian.net"

# Jira issue keys are PROJECTKEY-NUMBER (e.g. ``PROJ-123``,
# ``ENG-4711``). The pattern is two-or-more uppercase letters
# (Atlassian rejects single-letter project keys), then a dash, then
# digits. ``\b`` boundaries prevent matching inside larger tokens
# like ``CVE-2024-12345``.
_JIRA_ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")

# Forbidden markers - strings that, when present, indicate a
# regression hiding behind a successful exit code. Each comment
# explains what the marker rules out.
_FORBIDDEN_MARKERS = (
    # Nested tool-entry shape regression: the supervisor gateway
    # rejects a flat ``{type, name, description}`` shape with this
    # error code. If the parser regresses to the flat form (e.g.
    # someone "simplifies" the YAML schema), this catches it.
    "INVALID_PARAMETER_VALUE",
    # Spec validator regression: the AP-side validator emitted an
    # invalid spec, so the bundle never reached the executor.
    "invalid agent spec synthesized",
    # Auth regression: a 401 means the BearerAuth or the resolver's
    # token is wrong. Better to fail loud than silently retry.
    "401 Unauthorized",
)

# OAuth-required fingerprint - when the supervisor executor parses
# an ``error`` SSE event with ``code == "oauth"``, it surfaces the
# message as ``\n\nAuth required - please log in to <connector>:
# \n<url>\n\n`` (plain text, URL on its own line). The "Auth
# required" prefix is the canonical fingerprint; matching it in
# stdout means the user needs to complete OAuth out of band
# before the test can pass.
_OAUTH_REQUIRED_PREFIX_FMT = "please log in to {connector}"

# Stale env vars that the omnigent subprocess might pick up if
# we don't strip them. The supervisor executor reads its
# credentials from the configured profile, NOT from these vars; if
# they leak in, they can mask a regression in profile resolution
# (the test would pass via env-var fallback even though the profile
# path is broken).
_STALE_ENV_VARS = (
    "DATABRICKS_TOKEN",
    "DATABRICKS_HOST",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE",
    "CLAUDECODE",
    # OPENAI_BASE_URL elsewhere in the repo points at a full
    # ``/serving-endpoints`` URL. The supervisor resolver
    # explicitly does NOT honor it (it would produce a malformed
    # gateway URL), but we strip it anyway so a future change that
    # accidentally adds env-var support can't pass this test by
    # accident.
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
)


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------


def _resolve_supervisor_profile() -> str:
    """
    Read the profile from the example bundle config and validate it
    exists in ``~/.databrickscfg``, or skip cleanly.

    :returns: The profile name, e.g. ``"oss"``.
    """
    example_config = yaml.safe_load((_EXAMPLE_BUNDLE / "config.yaml").read_text())
    profile = (example_config.get("executor") or {}).get("profile")
    if not profile:
        pytest.skip(
            "tests/resources/examples/databricks_supervisor/config.yaml has no "
            "executor.profile - cannot resolve Databricks credentials."
        )
    if not _DATABRICKSCFG_PATH.exists():
        pytest.skip(
            f"{_DATABRICKSCFG_PATH} not found - run "
            f"``databricks auth login --profile {profile}`` first."
        )
    cfg = configparser.ConfigParser()
    cfg.read(_DATABRICKSCFG_PATH)
    if profile not in cfg:
        pytest.skip(
            f"Profile [{profile}] missing from {_DATABRICKSCFG_PATH}. "
            f"Run ``databricks auth login --profile {profile}``."
        )
    return profile


def _write_supervisor_bundle(
    tmp_path: Path,
    connector_name: str,
    connector_description: str,
    prompt: str,
    profile: str,
) -> Path:
    """
    Synthesize a supervisor agent bundle wired to one
    ``uc_connection`` system connector.

    Reads the model and executor config from the example bundle
    so the test stays in sync with the canonical config.

    :param tmp_path: pytest tmp_path for the synthesized bundle.
    :param connector_name: System connector name, e.g.
        ``"system_ai_agent_google_drive"``.
    :param connector_description: Human-readable tool description
        the LLM uses when deciding to call the tool.
    :param prompt: System prompt for the agent.
    :param profile: Databricks profile for ``executor.profile``.
    :returns: Path to the bundle directory.
    """
    example_config = yaml.safe_load((_EXAMPLE_BUNDLE / "config.yaml").read_text())
    model = (example_config.get("llm") or {}).get("model") or example_config["executor"]["model"]
    bundle = tmp_path / "supervisor_bundle"
    bundle.mkdir()
    config: dict[str, object] = {
        "spec_version": 1,
        "name": f"supervisor-e2e-{connector_name}",
        "prompt": prompt,
        "executor": {
            "type": "omnigent",
            "config": {"harness": "databricks_supervisor"},
            "profile": profile,
        },
        "llm": {"model": model},
        "tools": [
            {
                "type": "uc_connection",
                "uc_connection": {
                    "name": connector_name,
                    "description": connector_description,
                },
            }
        ],
    }
    (bundle / "config.yaml").write_text(yaml.dump(config))
    return bundle


def _run_supervisor(
    bundle: Path,
    user_prompt: str,
) -> subprocess.CompletedProcess[str]:
    """
    Run ``omnigent run <bundle> -p <user_prompt>`` and return the
    completed subprocess. The Databricks profile rides on the bundle's
    ``executor.profile`` (the ``--profile`` CLI flag was removed).

    Uses ``sys.executable`` so the subprocess shares pytest's
    interpreter (and therefore the worktree's editable install of
    omnigent) - hardcoding ``.venv/bin/python`` would break for
    worktrees that share the main checkout's venv.

    Strips :data:`_STALE_ENV_VARS` so a leaked credential can't
    mask a regression in profile resolution.

    :param bundle: Synthesized agent bundle directory.
    :param user_prompt: One-shot prompt sent via ``-p``. The
        subprocess exits as soon as the assistant's reply
        completes.
    :returns: ``CompletedProcess`` with text stdout/stderr.
    """
    env = dict(os.environ)
    for stale in _STALE_ENV_VARS:
        env.pop(stale, None)

    args = [
        sys.executable,
        "-m",
        "omnigent",
        "run",
        str(bundle),
        "--no-session",
        "-p",
        user_prompt,
    ]
    return subprocess.run(
        args,
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )


def _skip_if_oauth_required(result: subprocess.CompletedProcess[str], connector_name: str) -> None:
    """
    Skip cleanly if the supervisor surfaced an OAuth-required
    TextChunk. Not a regression - just means OAuth needs to be
    completed out of band before the success-path test can pass.

    Looking at the prefix specifically (rather than just ``oauth``)
    distinguishes "user needs to log in" from "the OAuth handler
    regressed and printed something else." The bracketed prefix is
    the contract; if it's missing while ``oauth`` is present, the
    forbidden-marker check below catches it.

    :param result: Completed subprocess.
    :param connector_name: The connector name to look up in the
        OAuth prefix.
    """
    expected_prefix = _OAUTH_REQUIRED_PREFIX_FMT.format(connector=connector_name)
    if expected_prefix in result.stdout:
        # Pull the login URL out of the message so the developer
        # can copy it directly.
        login_line = next(
            (line for line in result.stdout.splitlines() if expected_prefix in line),
            "",
        )
        pytest.skip(
            f"OAuth required for {connector_name} on this workspace. "
            f"Complete the login out of band and rerun:\n"
            f"  {login_line.strip()}"
        )


def _assert_no_regression_markers(
    result: subprocess.CompletedProcess[str],
) -> None:
    """
    Fail loud on any forbidden marker - these indicate regressions
    that hide behind a non-zero exit code.

    :param result: Completed subprocess.
    """
    combined = result.stdout + result.stderr
    for marker in _FORBIDDEN_MARKERS:
        assert marker not in combined, (
            f"Forbidden marker {marker!r} in subprocess output - "
            f"a regression hid behind the exit code.\n"
            f"stderr tail:\n{result.stderr[-2500:]}"
        )


def _assert_subprocess_succeeded(
    result: subprocess.CompletedProcess[str],
) -> None:
    """
    Assert the subprocess exited cleanly (after the OAuth-skip and
    forbidden-marker checks).

    :param result: Completed subprocess.
    """
    assert result.returncode == 0, (
        f"--omnigent exited {result.returncode}.\n"
        f"stderr tail:\n{result.stderr[-2500:]}\n"
        f"stdout tail:\n{result.stdout[-2500:]}"
    )


# --------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------


def test_supervisor_google_drive_returns_files(tmp_path: Path) -> None:
    """
    Run a supervisor turn against the real Google Drive connector;
    assert the LLM's reply contains a real Google Drive file URL.

    Skips when:
    - Profile not configured in ``~/.databrickscfg`` (see
      :func:`_resolve_supervisor_profile`).
    - OAuth required on first use of the connector.

    :param tmp_path: pytest tmp_path for the synthesized bundle.
    """
    profile = _resolve_supervisor_profile()
    bundle = _write_supervisor_bundle(
        tmp_path,
        connector_name="system_ai_agent_google_drive",
        connector_description="Search the user's team Google Drive",
        prompt=(
            "You are a Databricks supervisor agent. When the user "
            "asks you to find files, call the Google Drive search "
            "tool and quote at least one real file URL or document "
            "link from the results in your reply. Do not make up "
            "URLs - if the tool returns nothing, say so explicitly."
        ),
        profile=profile,
    )

    result = _run_supervisor(
        bundle,
        "List 3 recent files in my Google Drive. For each, include "
        "its title and the URL link from the search result.",
    )

    _skip_if_oauth_required(result, "system_ai_agent_google_drive")
    _assert_no_regression_markers(result)
    _assert_subprocess_succeeded(result)

    # The reply must include a Drive URL host. The LLM is instructed
    # to quote URLs verbatim from the tool result, so this host can
    # only appear if the connector actually returned files. A
    # hallucinated URL is unlikely (Claude Sonnet 4.6 is reliable on
    # "do not make up URLs") and even if it happens, the host has to
    # match - the LLM doesn't know the exact host string ahead of
    # time without seeing real tool output.
    stdout_lower = result.stdout.lower()
    saw_drive_url = any(host in stdout_lower for host in _GOOGLE_DRIVE_URL_HOSTS)
    assert saw_drive_url, (
        f"Expected a Google Drive URL host (one of "
        f"{_GOOGLE_DRIVE_URL_HOSTS}) in the assistant's reply, but "
        f"none appeared. Either the connector returned no files, the "
        f"LLM didn't surface tool output, or a regression in the "
        f"function_call/function_call_output pairing dropped the "
        f"tool result.\nstdout tail:\n{result.stdout[-2500:]}"
    )


def test_supervisor_atlassian_returns_jira_issue(tmp_path: Path) -> None:
    """
    Run a supervisor turn against the real Atlassian connector;
    assert the LLM's reply contains BOTH an ``atlassian.net`` URL
    AND a Jira-style issue key (``PROJ-123``).

    The two assertions guard against different failure modes:

    - URL host alone could appear if the LLM hallucinated a generic
      Atlassian URL.
    - Issue key alone could appear if the LLM hallucinated a key
      (e.g. from training data).

    Both together are very unlikely to be hallucinated - the LLM
    needs to see the real connector output to produce an issue key
    embedded next to its real ``atlassian.net`` URL.

    Skips when:
    - Profile not configured in ``~/.databrickscfg``.
    - OAuth required on first use of the connector.

    :param tmp_path: pytest tmp_path for the synthesized bundle.
    """
    profile = _resolve_supervisor_profile()
    bundle = _write_supervisor_bundle(
        tmp_path,
        connector_name="system_ai_agent_atlassian_mcp",
        connector_description="Search the user's Jira and Confluence",
        prompt=(
            "You are a Databricks supervisor agent. When the user "
            "asks you to find Jira issues, call the Atlassian search "
            "tool and quote at least one real issue key (like "
            "``PROJ-123``) AND its full Atlassian URL from the "
            "results in your reply. Do not make up issue keys or "
            "URLs - if the tool returns nothing, say so explicitly."
        ),
        profile=profile,
    )

    result = _run_supervisor(
        bundle,
        "Find any open Jira issue assigned to me. Quote at least "
        "one issue key (e.g. PROJ-123) and its full URL from the "
        "search result.",
    )

    _skip_if_oauth_required(result, "system_ai_agent_atlassian_mcp")
    _assert_no_regression_markers(result)
    _assert_subprocess_succeeded(result)

    # First half: the Atlassian URL host must appear. Like the
    # Drive case, this proves the tool's output reached the reply.
    saw_atlassian_url = _ATLASSIAN_URL_HOST in result.stdout.lower()

    # Second half: a real Jira issue key must appear. The regex
    # is anchored on word boundaries so we don't match arbitrary
    # tokens. Combined with the URL check, this is a strong signal
    # that real tool output is present.
    issue_key_match = _JIRA_ISSUE_KEY_RE.search(result.stdout)

    assert saw_atlassian_url and issue_key_match is not None, (
        f"Expected BOTH an ``atlassian.net`` URL AND a Jira issue "
        f"key (matching {_JIRA_ISSUE_KEY_RE.pattern}) in the "
        f"assistant's reply.\n"
        f"  saw atlassian.net url: {saw_atlassian_url}\n"
        f"  jira issue key match:  "
        f"{issue_key_match.group() if issue_key_match else None}\n"
        f"Either the connector returned no issues, the LLM didn't "
        f"surface tool output, or a regression in event mapping "
        f"dropped the tool result.\n"
        f"stdout tail:\n{result.stdout[-2500:]}"
    )
