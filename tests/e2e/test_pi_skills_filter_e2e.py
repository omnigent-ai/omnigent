"""E2E test: pi executor's ``skills:`` field actually filters which
skills the agent sees.

The Pi executor translates ``skills_filter`` into Pi CLI args at
construction time (``_resolve_pi_skill_args``):

- ``"all"``  → ``--skill <bundle_path>`` for each bundle skill, no
               ``--no-skills`` (Pi host auto-discovery stays on).
- ``"none"`` → ``["--no-skills"]`` (suppresses everything).
- list[name] → ``["--no-skills"]`` plus one ``--skill <bundle_path>``
               per named bundle skill (silent skip for missing names).

This test parametrizes the three filter modes against three fixture
agent bundles whose ``skills/`` subdir ships two distinctively-named
SKILL.md files:

- ``pi-e2e-xyz-greet-c4a8d5``
- ``pi-e2e-xyz-count-d2f6e1``

(Hyphens, not underscores — Pi's skill spec requires hyphenated
directory names.) The unique suffixes (``c4a8d5`` / ``d2f6e1``) are
unforgable — the model cannot hallucinate them, so a string match in
the agent's enumerated output is unambiguous proof Pi actually loaded
that skill.

This is the live sibling of ``test_codex_skills_filter_e2e.py`` and
drives the same live-server session flow. The Pi arg-resolution logic
itself is additionally unit-pinned in
``tests/inner/test_pi_executor.py::test_resolve_pi_skill_args_*``.

Usage::

    pytest tests/e2e/test_pi_skills_filter_e2e.py \
        --profile <databricks-profile> -v
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
    send_user_message_to_session,
    upload_agent,
)

_FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "resources" / "agents"

pytestmark = pytest.mark.skipif(
    (reason := cli_unavailable_reason("pi")) is not None,
    reason=(
        f"pi skills e2e requires a runnable 'pi' CLI; {reason}. Install/fix Pi to run this module."
    ),
)

# The two bundled skill names. Suffixes are intentionally distinctive
# so the assertions are unambiguous — if these strings show up in the
# model's response, Pi genuinely surfaced them.
_GREET_NAME = "pi-e2e-xyz-greet-c4a8d5"
_COUNT_NAME = "pi-e2e-xyz-count-d2f6e1"


def _extract_all_text(body: dict[str, Any]) -> str:
    """
    Concatenate all message text blocks from a response body.

    :param body: The terminal response body returned by
        :func:`tests.e2e.conftest.poll_session_until_terminal`.
    :returns: All assistant text joined by newlines.
    """
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


def _enumerate_skills_with_retry(
    http_client: httpx.Client,
    session_id: str,
    *,
    max_attempts: int = 3,
) -> str:
    """
    Enumerate the agent's skills, retrying on a transient empty turn.

    A harness occasionally completes a turn with ``output: []`` — an
    empty model completion that carries no skill info, so resend up to
    *max_attempts* times until the agent produces text. This can't mask
    a broken filter: wrong-skill output breaks the loop on the first
    non-empty turn (assertions fire as normal), and a filter that stays
    empty drains the retries and fails ``expected_visible`` on empty
    text.

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
    ``~/.databrickscfg`` profile names. At test time we materialize a
    per-test copy with the actual ``--profile`` baked in. Without a
    profile the Pi harness wrap can't authenticate with the Databricks
    gateway and the agent run fails before skills are even consulted.

    :param src_dir: Path to the fixture under
        ``tests/resources/agents/pi_skills_*/``.
    :param dst_dir: Tmp directory to copy into.
    :param profile: Databricks profile name from ``--profile``, e.g.
        ``"test-profile"``.
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
def pi_profile(request: pytest.FixtureRequest) -> str:
    """
    Return the ``--profile`` CLI arg, or skip if not provided.

    :param request: Pytest request object.
    :returns: The Databricks profile name.
    :raises pytest.skip.Exception: If ``--profile`` was not passed.
    """
    profile: str = request.config.getoption("--profile")
    if not profile:
        pytest.skip(
            "pi skills e2e requires --profile <name> "
            "(e.g. --profile test-profile) so the harness wrap can "
            "authenticate the Databricks gateway"
        )
    return profile


@pytest.mark.parametrize(
    "fixture, expected_visible, expected_hidden",
    [
        # ``skills: all`` → both bundled skills exposed via
        # ``--skill <path>`` flags. Failure mode: the resolver drops
        # the bundle source, the env-var bridge drops the bundle dir,
        # or the ``"all"`` branch is broken. Any of these would leave
        # the agent with zero bundle skills and the ``in text``
        # assertion would fail.
        (
            "pi_skills_all",
            [_GREET_NAME, _COUNT_NAME],
            [],
        ),
        # ``skills: none`` → ``--no-skills`` suppresses both
        # auto-discovery and explicit skills. Failure mode: the
        # env-var bridge drops the filter, the harness wrap defaults
        # to ``"all"``, or the ``"none"`` branch emits stray
        # ``--skill`` flags. Any of these would leak the bundle skills
        # and the ``not in text`` assertion would fail.
        (
            "pi_skills_none",
            [],
            [_GREET_NAME, _COUNT_NAME],
        ),
        # ``skills: [greet]`` → ``--no-skills`` plus exactly one
        # ``--skill`` for the named bundle skill. Failure mode: the
        # per-name filter doesn't apply (counter leaks), or it applies
        # but emits the wrong path (greet missing). Either is caught by
        # one of the two assertions.
        (
            "pi_skills_list",
            [_GREET_NAME],
            [_COUNT_NAME],
        ),
    ],
)
def test_pi_skills_filter_e2e(
    http_client: httpx.Client,
    pi_profile: str,
    live_runner_id: str,
    fixture: str,
    expected_visible: list[str],
    expected_hidden: list[str],
    tmp_path: Path,
) -> None:
    """
    Pi's ``skills:`` filter actually controls what the model sees.

    Live e2e regression-pin for the Pi skills bridge. Loaded with
    deterministic-name fixtures (suffixes unforgable by the LLM) so the
    assertions can string-match without an LLM judge: the presence of
    ``pi-e2e-xyz-greet-c4a8d5`` in the model's output is unambiguous
    proof Pi actually loaded that skill via the ``--skill`` flag its
    ``_resolve_pi_skill_args`` emitted.

    **What breaks if the feature is wrong:**

    - If the ``"all"`` branch drops the bundle source (or the
      AP→harness env-var bridge drops the bundle dir / filter), the
      ``"all"`` and ``"list"`` cases find no bundle-skill names in the
      output → ``expected_visible`` assertion fires.
    - If the filter defaults to ``"all"`` when the env-var bridge
      breaks, the ``"none"`` case leaks bundle skills →
      ``expected_hidden`` assertion fires with the leaked name.
    - If the per-name list filter is broken (matches everything or
      nothing), the ``"list"`` case fires either branch.

    Each breakage produces a specific failure message naming the
    offending skill, so triage can jump straight to the right layer.

    :param http_client: The session-scoped ``httpx.Client`` from
        ``tests.e2e.conftest``, pointed at a live Omnigent server.
    :param pi_profile: Databricks profile name from ``--profile``.
    :param live_runner_id: Id of the live runner the session binds to.
    :param fixture: Name of the fixture agent dir under
        ``tests/resources/agents/`` whose ``skills:`` value determines
        what the agent is allowed to see.
    :param expected_visible: Bundled skill names that MUST appear in
        the agent's output (string contains).
    :param expected_hidden: Bundled skill names that MUST NOT appear in
        the agent's output (string-not-contains, scoped to our
        fixture's distinctive names so the user's host skills don't
        pollute the assertion).
    """
    bundle = _materialize_with_profile(_FIXTURE_ROOT / fixture, tmp_path, pi_profile)
    agent = upload_agent(http_client, bundle)

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent,
        runner_id=live_runner_id,
    )
    text = _enumerate_skills_with_retry(http_client, session_id)

    # Visibility assertions — the listed skill names MUST appear. If a
    # name is absent, Pi didn't load that skill, which means either the
    # resolver didn't emit ``--skill`` for the bundle source or the
    # AP→harness env-var bridge dropped the bundle dir / filter.
    for name in expected_visible:
        assert name in text, (
            f"fixture={fixture!r}: bundle skill {name!r} should be visible "
            f"to the agent but didn't appear in the enumerated output. "
            f"Likely the Pi resolver didn't emit ``--skill`` for the bundle "
            f"source, or the AP->harness env-var bridge dropped the bundle "
            f"dir / skills filter. Agent output:\n{text[:1500]}"
        )

    # Suppression assertions — the listed names MUST NOT appear. We
    # only assert on OUR distinctive skill names (with the ``c4a8d5`` /
    # ``d2f6e1`` suffix); the user's host skills may legitimately
    # surface in the output but won't match our suffixed names, so the
    # assertion stays clean.
    for name in expected_hidden:
        assert name not in text, (
            f"fixture={fixture!r}: bundle skill {name!r} should be HIDDEN "
            f"from the agent but appeared in the output. The "
            f"``skills: {fixture.removeprefix('pi_skills_')!r}`` filter "
            f"didn't suppress this skill — likely the env-var bridge "
            f'dropped the filter, the harness wrap fell back to ``"all"``, '
            f"or the per-name filter isn't checking each name. "
            f"Agent output:\n{text[:1500]}"
        )
