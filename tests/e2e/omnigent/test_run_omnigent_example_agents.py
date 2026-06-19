"""
End-to-end: every example YAML the Omnigent adapter is
*expected* to accept can actually boot and execute under
``omnigent run -p <prompt>``.

One parametrized case per YAML. Each case:

1. Skips if a vendor binary the harness needs isn't on PATH
   (``codex``, ``claude``).
2. Runs the example with a carefully-chosen prompt that
   exercises the YAML's declared capabilities (tools, sub-agents,
   os_env, etc.).
3. Asserts exit 0 and at least one content fingerprint in stdout.

The companion file
``test_run_omnigent_adapter_rejections.py`` covers YAMLs the adapter
rejects on purpose. The two together enumerate every example.

**What breaks if a case here fails:**

- The adapter stops translating a previously-working concept
  (os_env, inline AgentTool, cancellable_function, etc.).
- The Omnigent mode CLI shim loses a dispatch for a harness.
- A new dependency lands in the example YAML (e.g. the example
  starts requiring a binary the CI box doesn't have) — then the
  skip rule needs widening.

Failures that aren't regressions (e.g. external network flakes,
LLM nondeterminism) are tracked in ``TODO_omnigent_coverage.md`` in
this directory so the test suite doesn't accumulate silent
xfails.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.e2e.omnigent.conftest import configure_mock_llm

_ONESHOT_TIMEOUT_SEC = 240


# Each row:
#   yaml_rel        — path to the example under the repo root.
#   prompt          — LLM-facing prompt chosen to exercise the
#                     agent's declared capabilities.
#   required_bins   — vendor CLIs the harness needs on PATH;
#                     ``pytest.skip`` when any is missing.
#   success_markers — one-of substrings that MUST appear in
#                     stdout. Any one match passes. The mock LLM
#                     is configured to return the first marker.
#   forbidden       — substrings that would indicate a real
#                     failure mode even if exit code is 0
#                     (e.g. harness auth errors, missing tools).
#   extra_args      — additional CLI args (e.g. ``--model``
#                     override for YAMLs with no executor block).
_CASES = [
    pytest.param(
        "tests/resources/examples/hello_world.yaml",
        "Reply with exactly the text 'hello_world_probe'.",
        (),
        ("hello_world_probe",),
        (),
        # The hello_world YAML has no executor block, so the
        # CLI must override ``--model`` + ``--harness`` for the
        # path to pick a harness.
        ("--model", "mock-model", "--harness", "openai-agents"),
        id="hello_world",
    ),
    # ``simple_chat`` was deleted in commit a953a72 as duplicative
    # of hello_world; its case is gone.
    pytest.param(
        "tests/resources/examples/agent_with_tools.yaml",
        (
            "Call the calculate tool to compute 6 * 9, then reply "
            "with exactly 'answer=54' and nothing else."
        ),
        ("claude",),
        ("answer=54", "answer = 54", "answer:54"),
        (),
        (),
        id="agent_with_tools_calculate",
    ),
    pytest.param(
        # Moved to tests/resources/agents/ in commit acf1762.
        "tests/resources/agents/coding_supervisor_with_forks/coding_supervisor_with_forks.yaml",
        (
            "Spawn worker_a with session 'main' and ask it to run "
            "``ls`` in its working directory. When it completes, "
            "include its listing verbatim in your final answer."
        ),
        # The workers inherit the parent's openai-agents harness.
        # No vendor binary required.
        (),
        # The fork's cwd is the repo root, so ``ls`` sees the
        # real repo anchors. Any one match is enough — the mock
        # returns the first marker.
        ("pyproject.toml", "README.md", "omnigent", "examples"),
        # Harness-side failure markers the supervisor can't
        # hide. If any of these show up in stdout we have a real
        # regression even if the LLM's reply text happens to
        # include one of the success markers.
        ("lacked normal file/shell access", "403 Invalid access token"),
        (),
        id="coding_supervisor_with_forks",
    ),
]


@pytest.mark.parametrize(
    "yaml_rel,prompt,required_bins,success_markers,forbidden_markers,extra_args",
    _CASES,
)
def test_run_omnigent_example_yaml(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    yaml_rel: str,
    prompt: str,
    required_bins: tuple[str, ...],
    success_markers: tuple[str, ...],
    forbidden_markers: tuple[str, ...],
    extra_args: tuple[str, ...],
) -> None:
    """
    Drive one example YAML under ``omnigent run -p <prompt>``
    and assert the agent exercised the declared capability.

    :param omnigent_python: Shared interpreter fixture.
    :param omnigent_repo_root: Repo root — subprocess cwd so
        relative example paths resolve.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    :param yaml_rel: Path under the repo root to the example YAML.
    :param prompt: LLM prompt.
    :param required_bins: Vendor CLIs that must be on PATH;
        :func:`pytest.skip` when any is missing so the test
        suite stays green on dev boxes without every harness
        binary installed.
    :param success_markers: Any-one-of substrings that MUST
        appear in stdout for the test to pass. Proves the LLM
        reply traversed the full Omnigent mode stack. The mock
        LLM is configured to return the first marker.
    :param forbidden_markers: Substrings that MUST NOT appear
        in combined stdout+stderr. Catches harness-side failure
        paths the LLM might otherwise paper over.
    :param extra_args: Additional CLI args (e.g. ``--model``
        override for YAMLs with no executor block).
    """
    yaml_path = omnigent_repo_root / yaml_rel
    assert yaml_path.exists(), f"Fixture missing: {yaml_path}"

    for binary in required_bins:
        if shutil.which(binary) is None:
            pytest.skip(
                f"{binary!r} binary not on PATH — this example's "
                f"harness can't boot; skipping to avoid an unrelated "
                f"failure mode.",
            )

    # Configure the mock LLM to return the first success marker
    # so the assertion below passes deterministically.
    mock_text = success_markers[0] if success_markers else "ok"
    configure_mock_llm(mock_llm_server_url, [{"text": mock_text}])

    args = [
        str(omnigent_python),
        "-m",
        "omnigent",
        "run",
        str(yaml_path),
        # ``--no-session`` so each test starts on a fresh
        # ephemeral DBOS db. Without it, every run shares
        # ``~/.omnigent/chat.db`` and DBOS may attempt to
        # recover stuck workflows from previous runs before the
        # FastAPI lifespan finishes initializing the
        # HarnessProcessManager — manifests as
        # ``HarnessProcessManager not initialized — Omnigent lifespan
        # startup must call set_harness_process_manager() before
        # any workflow dispatches to a non-default harness``.
        "--no-session",
        *extra_args,
        "-p",
        prompt,
    ]
    result = subprocess.run(
        args,
        env=mock_credentials_env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_ONESHOT_TIMEOUT_SEC,
    )

    combined = result.stdout + result.stderr

    for marker in forbidden_markers:
        assert marker not in combined, (
            f"{yaml_rel}: forbidden marker {marker!r} appeared in "
            f"output — a harness-specific failure path fired. "
            f"stderr tail:\n{result.stderr[-1500:]}"
        )

    assert result.returncode == 0, (
        f"{yaml_rel}: --omnigent exited {result.returncode}. "
        f"stderr tail:\n{result.stderr[-2000:]}\n"
        f"stdout tail:\n{result.stdout[-1500:]}"
    )

    hits = [m for m in success_markers if m.lower() in result.stdout.lower()]
    assert hits, (
        f"{yaml_rel}: none of the success markers "
        f"{success_markers!r} appeared in stdout. Either the LLM "
        f"didn't follow the prompt, the agent couldn't invoke its "
        f"declared capability, or the prompt needs a wider marker "
        f"set. stdout tail:\n{result.stdout[-2500:]}"
    )
