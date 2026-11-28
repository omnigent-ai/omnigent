"""E2E test: codex executor's ``skills:`` field actually filters
which skills the agent sees.

The codex executor populates a per-conversation ``$CODEX_HOME/skills/``
with symlinks to bundle / host skill directories based on the spec's
``skills_filter`` (``"all"`` / ``"none"`` / list). Codex itself
auto-discovers skills under ``$CODEX_HOME/skills/<name>/SKILL.md`` and
injects each skill's name + description into the system prompt.

This test parametrizes the three filter modes against three fixture
agent bundles whose ``skills/`` subdir ships two distinctively-named
SKILL.md files:

- ``codex_e2e_xyz_greet_a3f9c2``
- ``codex_e2e_xyz_count_b8d4e7``

The unique suffixes (``a3f9c2`` / ``b8d4e7``) are unforgable — GPT
cannot hallucinate them, so a string match in the agent's enumerated
output is unambiguous proof codex actually loaded that skill.

Usage::

    pytest tests/e2e/test_codex_skills_filter_e2e.py \
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests._helpers.messages import all_message_text as _extract_all_text
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
    send_user_message_to_session,
    upload_agent,
)

_FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "resources" / "agents"

pytestmark = pytest.mark.skipif(
    (reason := cli_unavailable_reason("codex")) is not None,
    reason=(
        "codex skills e2e requires a runnable 'codex' CLI; "
        f"{reason}. Install/fix Codex to run this module."
    ),
)

# The two bundled skill names. Suffixes are intentionally
# distinctive so the assertions are unambiguous — if these strings
# show up in the model's response, codex genuinely surfaced them.
_GREET_NAME = "codex_e2e_xyz_greet_a3f9c2"
_COUNT_NAME = "codex_e2e_xyz_count_b8d4e7"


def _enumerate_skills_with_retry(
    http_client: httpx.Client,
    session_id: str,
    *,
    max_attempts: int = 3,
) -> str:
    """
    Enumerate the agent's skills, retrying on a transient empty turn.

    Codex occasionally completes a turn with ``output: []`` — an empty
    model completion that carries no skill info, so resend up to
    *max_attempts* times until the agent produces text. This can't mask a
    broken filter: wrong-skill output breaks the loop on the first
    non-empty turn (assertions fire as normal), and a filter that stays
    empty drains the retries and fails ``expected_visible`` on empty text.

    :returns: The first non-empty assistant text, else the last
        (empty) attempt.
    """
    content = (
        "List every skill name available to you in this session. "
        "Output ONLY the names, one per line, exactly as they appear "
        "in your environment — do not paraphrase, do not abbreviate, "
        "do not invent skills you do not see. If you have no skills, "
        "output the literal string `NO_SKILLS_LOADED`."
    )
    body: dict[str, Any] = {}
    text = ""
    for _ in range(max_attempts):
        response_id = send_user_message_to_session(
            http_client,
            session_id=session_id,
            content=content,
        )
        body = poll_session_until_terminal(
            http_client,
            session_id=session_id,
            response_id=response_id,
            timeout=120,
        )
        # A non-completed / errored turn is a genuine failure (harness
        # crash, auth error), not the empty-completion flake — surface
        # it immediately rather than burning retries on it.
        assert body["status"] == "completed", (
            f"agent run failed: status={body.get('status')!r} error={body.get('error')!r}"
        )
        text = _extract_all_text(body)
        if text.strip():
            break
    return text


def _materialize_with_profile(
    src_dir: Path,
    dst_dir: Path,
    profile: str,
) -> Path:
    """
    Copy a fixture agent bundle and inject the Databricks profile.

    The fixture YAMLs intentionally omit ``executor.profile`` so the
    same fixtures work across developers with different
    ``~/.databrickscfg`` profile names. At test time we materialize
    a per-test copy with the actual ``--profile`` baked in. Without
    a profile the codex harness wrap can't authenticate with the
    Databricks gateway and the agent run fails before skills are
    even consulted.

    :param src_dir: Path to the fixture under
        ``tests/resources/agents/codex_skills_*/``.
    :param dst_dir: Tmp directory to copy into.
    :param profile: Databricks profile name from
        ``--profile``, e.g. ``"test-profile"``.
    :returns: The materialized bundle directory ready for
        :func:`upload_agent`.
    """
    bundle = dst_dir / src_dir.name
    shutil.copytree(src_dir, bundle)
    yaml_path = bundle / f"{src_dir.name}.yaml"
    raw = yaml.safe_load(yaml_path.read_text())
    raw["executor"]["profile"] = profile
    yaml_path.write_text(yaml.safe_dump(raw, default_flow_style=False))
    return bundle


@pytest.fixture
def codex_profile(request: pytest.FixtureRequest) -> str:
    """
    Return the ``--profile`` CLI arg, or skip if not provided.

    :param request: Pytest request object.
    :returns: The Databricks profile name.
    :raises pytest.skip.Exception: If ``--profile`` was not passed.
    """
    profile: str = request.config.getoption("--profile")
    if not profile:
        pytest.skip(
            "codex skills e2e requires --profile <name> "
            "(e.g. --profile test-profile) so the harness wrap can "
            "authenticate the Databricks gateway"
        )
    return profile


@pytest.mark.parametrize(
    "fixture, expected_visible, expected_hidden",
    [
        # ``skills: all`` → both bundled skills exposed.
        # Failure mode: populator skips the bundle source, or the
        # ``HARNESS_CODEX_BUNDLE_DIR`` env-var bridge drops the
        # workdir, or the populator's ``"all"`` branch is broken.
        # Any of these would leave the agent with zero bundle
        # skills and the ``in text`` assertion would fail.
        (
            "codex_skills_all",
            [_GREET_NAME, _COUNT_NAME],
            [],
        ),
        # ``skills: none`` → neither bundled skill exposed.
        # Failure mode: the env-var bridge drops
        # ``HARNESS_CODEX_SKILLS_FILTER``, the harness wrap
        # defaults to ``"all"``, the populator's ``"none"`` branch
        # creates the directory anyway. Any of these would leak the
        # bundle skills into the agent's index and the ``not in
        # text`` assertion would fail with the leaked name.
        (
            "codex_skills_none",
            [],
            [_GREET_NAME, _COUNT_NAME],
        ),
        # ``skills: [greet]`` → only the named skill exposed.
        # Failure mode: per-name filter doesn't apply (counter
        # leaks), or filter applies but uses wrong source priority
        # (greet missing because populator looked only at host).
        # Either failure is caught by one of the two assertions.
        (
            "codex_skills_list",
            [_GREET_NAME],
            [_COUNT_NAME],
        ),
    ],
)
def test_codex_skills_filter_e2e(
    http_client: httpx.Client,
    codex_profile: str,
    live_runner_id: str,
    fixture: str,
    expected_visible: list[str],
    expected_hidden: list[str],
    tmp_path: Path,
) -> None:
    """
    Codex's ``skills:`` filter actually controls what the model sees.

    This is the live e2e regression-pin for the codex skills bridge.
    Loaded with deterministic-name fixtures (suffixes unforgable by
    the LLM) so the assertions can string-match without an LLM judge:
    the presence of ``codex_e2e_xyz_greet_a3f9c2`` in the model's
    output is unambiguous proof codex auto-discovered that skill in
    ``$CODEX_HOME/skills/`` and injected it into the system prompt's
    skill index.

    **What breaks if the feature is wrong:**

    - If ``_populate_codex_skills`` doesn't symlink bundle skills
      (e.g. the bundle source is dropped or the ``"all"`` branch
      broken), the ``"all"`` and ``"list"`` cases find no
      bundle-skill names in the output → ``expected_visible``
      assertion fires with a clear ``not found`` message.
    - If ``_resolve_skills_filter`` defaults to ``"all"`` when the
      AP-side env-var bridge breaks (the original pre-fix
      regression), the ``"none"`` case leaks bundle skills →
      ``expected_hidden`` assertion fires with the leaked name.
    - If the per-name list filter is broken (matches everything or
      matches nothing), the ``"list"`` case fires either branch.
    - If the populator's source priority is broken, the ``"list"``
      case fails because the named skill resolves to neither the
      bundle nor host.

    Each of those breakages produces a specific failure message that
    names the offending skill, so a regression triage can jump
    straight to the right layer.

    :param http_client: The session-scoped ``httpx.Client`` from
        ``tests.e2e.conftest``, pointed at a live Omnigent server.
    :param fixture: Name of the fixture agent dir under
        ``tests/resources/agents/`` whose ``skills:`` value
        determines what the agent is allowed to see.
    :param expected_visible: Bundled skill names that MUST appear in
        the agent's output (string contains).
    :param expected_hidden: Bundled skill names that MUST NOT appear
        in the agent's output (string-not-contains, scoped to our
        fixture's distinctive names so the user's host
        ``~/.codex/skills/`` doesn't pollute the assertion).
    """
    bundle = _materialize_with_profile(_FIXTURE_ROOT / fixture, tmp_path, codex_profile)
    agent = upload_agent(http_client, bundle)

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent,
        runner_id=live_runner_id,
    )
    text = _enumerate_skills_with_retry(http_client, session_id)

    # Visibility assertions — the listed skill names MUST appear.
    # If a name is absent, codex didn't auto-discover that skill,
    # which means either the populator didn't materialize it under
    # ``$CODEX_HOME/skills/<name>/`` or codex's discovery walk
    # didn't see it. Both indicate a bridge regression.
    for name in expected_visible:
        assert name in text, (
            f"fixture={fixture!r}: bundle skill {name!r} should be visible "
            f"to the agent but didn't appear in the enumerated output. "
            f"Likely the codex populator didn't symlink the bundle source, "
            f"or the AP→harness env-var bridge dropped "
            f"HARNESS_CODEX_BUNDLE_DIR / HARNESS_CODEX_SKILLS_FILTER. "
            f"Agent output:\n{text[:1500]}"
        )

    # Suppression assertions — the listed names MUST NOT appear.
    # We only assert on OUR distinctive skill names (with the
    # ``a3f9c2`` / ``b8d4e7`` suffix); the user's host
    # ``~/.codex/skills/`` may legitimately contain other skills
    # that surface in the agent's output, but those won't match our
    # suffixed names so the assertion stays clean.
    for name in expected_hidden:
        assert name not in text, (
            f"fixture={fixture!r}: bundle skill {name!r} should be HIDDEN "
            f"from the agent but appeared in the output. The "
            f"``skills: {fixture.removeprefix('codex_skills_')!r}`` filter "
            f"didn't suppress this skill — likely the env-var bridge "
            f"dropped HARNESS_CODEX_SKILLS_FILTER, the harness wrap "
            f'fell back to ``"all"``, or the per-name filter isn\'t '
            f"checking each name. Agent output:\n{text[:1500]}"
        )
