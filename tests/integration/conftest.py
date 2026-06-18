"""Fixtures for the per-harness journey suite.

This suite runs a small set of real-server, real-LLM journeys once per
wrapped harness (claude-sdk / codex / openai-agents). One harness per
pytest invocation, selected by ``--harness`` with the model pinned by
``--model`` (nightly.yml runs one matrix leg per harness)::

    pytest tests/integration/ --integration \\
        --harness claude-sdk --model databricks-claude-sonnet-4-6 \\
        --profile <name> --llm-api-key $KEY -v

Not to be confused with ``tests/server/integration/`` (mock-LLM server
integration tests that run in the default CI suite). This directory is
excluded from the default run via ``--ignore=tests/integration`` in
pyproject.toml and additionally gated on the ``--integration`` flag.

The live-server stack is reused from ``tests/e2e/conftest.py`` by
importing its fixture functions; pytest treats them as local fixtures
of this package.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx
import pytest

from tests import _model_pools
from tests.e2e._harness_probes import skip_if_harness_cli_missing
from tests.e2e.conftest import (  # noqa: F401  (re-exported pytest fixtures)
    create_runner_bound_session,
    databricks_workspace_host,
    http_client,
    live_runner_id,
    live_server,
    llm_api_key,
    mock_llm_server_url,
    register_inline_agent,
    using_mock_llm,
)
from tests.integration.model_selection import resolve_default_model

# Harnesses the journey suite supports. The legacy ``--harness``
# default ("databricks") is deliberately NOT accepted: each invocation
# must say which wrapped harness it is exercising.
_SUPPORTED_HARNESSES = frozenset({"claude-sdk", "codex", "openai-agents"})


def _is_mock_mode(config: pytest.Config) -> bool:
    """Return True when no real ``--llm-api-key`` was provided.

    :param config: Pytest config object.
    :returns: Whether mock LLM mode is active.
    """
    return config.getoption("--llm-api-key") is None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Gate the whole directory on ``--integration`` (real LLM + harness CLIs).

    When running in mock mode (no ``--llm-api-key``), the gate is
    lifted so the tests run without ``--integration``.

    On the codex harness, also rerun each journey up to 2x: codex
    multi-turn dispatch flakes in bursts (empty failed turns, the
    legacy class; burn-in failures were codex-only
    while claude-sdk / openai-agents stayed clean). Reruns resolve a
    different pool model per attempt via tests/_model_pools rotation.
    Per-attempt runtime stays under the CI --timeout=180 cap (turn
    polls are capped at 50s in helpers.run_turn), so a rerun never
    follows a thread-timeout hard kill.

    :param config: Pytest config object.
    :param items: Collected test items.
    """
    if not config.getoption("--integration") and not _is_mock_mode(config):
        marker = pytest.mark.skip(
            reason="Integration tests require --integration flag or mock mode (omit --llm-api-key)"
        )
        for item in items:
            item.add_marker(marker)
        return
    if config.getoption("--harness") == "codex":
        for item in items:
            item.add_marker(pytest.mark.flaky(reruns=2, reruns_delay=5))


@pytest.fixture(scope="session")
def harness_name(request: pytest.FixtureRequest) -> str:
    """The harness under test, from ``--harness``; fails loud on the default.

    In mock mode (no ``--llm-api-key``), defaults to ``"openai-agents"``
    when ``--harness`` is not explicitly set.

    :param request: Pytest fixture request.
    :returns: e.g. ``"claude-sdk"``.
    :raises pytest.UsageError: When ``--harness`` is absent or unsupported
        and not in mock mode.
    """
    harness: str = request.config.getoption("--harness")
    if harness not in _SUPPORTED_HARNESSES:
        if _is_mock_mode(request.config):
            return "openai-agents"
        raise pytest.UsageError(
            f"tests/integration/ requires an explicit --harness from "
            f"{sorted(_SUPPORTED_HARNESSES)}; got {harness!r}."
        )
    return harness


@pytest.fixture
def model_name(request: pytest.FixtureRequest, harness_name: str) -> str:
    """Resolve the model: param > ``model`` marker > ``--model``.

    In mock mode, defaults to ``"mock-model"``. Mirrors
    ``tests/inner/conftest.py``: explicit choices skip
    :mod:`tests._model_pools` spreading but still rotate on
    ``llm_flaky`` reruns. The workflow ``--model`` default is spread
    when ``OMNIGENT_TEST_MODEL_SPREAD`` is on, except for Codex: that
    leg is deliberately pinned to the higher-headroom gateway model.

    :param request: Pytest fixture request.
    :param harness_name: Harness under test, e.g. ``"codex"``.
    :returns: e.g. ``"databricks-claude-sonnet-4-6"``.
    """
    if _is_mock_mode(request.config):
        return "mock-model"
    if hasattr(request, "param") and request.param is not None:
        return _model_pools.resolve_model(request.param, spread=False)
    marker = request.node.get_closest_marker("model")
    if marker and marker.args:
        return _model_pools.resolve_model(marker.args[0], spread=False)
    return resolve_default_model(request.config.getoption("--model"), harness_name)


@pytest.fixture(autouse=True)
def _skip_when_cli_missing(request: pytest.FixtureRequest, harness_name: str) -> None:
    """Skip when the harness's CLI binary isn't installed locally.

    In mock mode, the harness CLI is not needed — the mock server
    handles all LLM calls directly, so this check is skipped.
    nightly.yml installs claude/codex; local machines may not have both.

    :param request: Pytest fixture request.
    :param harness_name: The harness under test.
    """
    if _is_mock_mode(request.config):
        return
    skip_if_harness_cli_missing(harness_name)


@dataclass
class JourneySession:
    """A per-test runner-bound session on the harness under test.

    :param agent_name: Registered inline agent name.
    :param session_id: Runner-bound session id, e.g. ``"conv_abc"``.
    """

    agent_name: str
    session_id: str


@pytest.fixture
def journey_session(
    http_client: httpx.Client,  # noqa: F811  (pytest fixture, not the import)
    live_runner_id: str,  # noqa: F811  (pytest fixture, not the import)
    harness_name: str,
    model_name: str,
    request: pytest.FixtureRequest,
    mock_llm_server_url: str | None,  # noqa: F811
) -> JourneySession:
    """Register a fresh inline agent + session for one journey test.

    Per-test unique agent names keep journeys independent on the shared
    session-scoped server.

    :param http_client: Identity-less client on the live server.
    :param live_runner_id: Runner to bind the session to.
    :param harness_name: Harness under test.
    :param model_name: Resolved model for this test.
    :param request: Pytest fixture request (for ``--profile``).
    :param mock_llm_server_url: Mock LLM server URL, or ``None``.
    :returns: The registered agent + bound session.
    """
    agent_name = register_inline_agent(
        http_client,
        name=f"journey-{harness_name}-{uuid.uuid4().hex[:6]}",
        harness=harness_name,
        model=model_name,
        profile=request.config.getoption("--profile"),
        prompt=(
            "You are a terse test assistant. Follow instructions "
            "exactly and literally. When asked to reply with a token, "
            "reply with the token text only."
        ),
        mock_llm_base_url=(
            f"{mock_llm_server_url}/v1"
            if _is_mock_mode(request.config) and mock_llm_server_url
            else None
        ),
    )
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    return JourneySession(agent_name=agent_name, session_id=session_id)
