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
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest

from tests import _model_pools
from tests.e2e._harness_probes import skip_if_harness_cli_missing
from tests.e2e.conftest import (  # noqa: F401  (re-exported pytest fixtures)
    _enforce_min_runner_version,
    _enforce_min_server_version,
    create_runner_bound_session,
    databricks_workspace_host,
    http_client,
    live_runner_id,
    live_server,
    llm_api_key,
    mock_llm_server_url,
    register_inline_agent,
    reset_mock_llm,
    server_version,
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
    """Gate the whole directory on mock mode (no ``--llm-api-key``).

    All tests run against the mock LLM server. Without ``--llm-api-key``
    the ``--integration`` flag is not required. If someone passes a real
    ``--llm-api-key`` without ``--integration`` the tests are skipped so
    they don't accidentally hit real credentials.

    :param config: Pytest config object.
    :param items: Collected test items.
    """
    if not config.getoption("--integration") and not _is_mock_mode(config):
        marker = pytest.mark.skip(
            reason="Integration tests require --integration flag or mock mode (omit --llm-api-key)"
        )
        for item in items:
            item.add_marker(marker)


@pytest.fixture(autouse=True, scope="function")
def _reset_mock_llm_between_tests(
    mock_llm_server_url: str | None,  # noqa: F811
) -> Iterator[None]:
    """Clear the shared mock-LLM queues before and after every test.

    The ``mock_llm_server_url`` fixture is session-scoped: the mock
    server's keyed response queues, captured requests, and gates
    persist across every test in a shard. ``_ResponseQueue.next()``
    silently returns a default ``"Mock LLM response"`` when exhausted
    and ``resolve_queue`` falls back to the ``"default"`` queue on a key
    miss, so a queue left non-empty (or keyed for another agent) by one
    test leaks scripted responses into its siblings — the recurring
    cause of ``test_smoke`` / ``test_multi_turn`` / ``test_sharing``
    breaking when run alongside the scripted round-trip tests.

    Resetting before *and* after each test makes every integration test
    start from a clean queue without a per-file opt-in fixture.
    ``reset_mock_llm`` is a no-op when the URL is ``None`` (real-LLM
    mode never starts the server fixture lazily) and the server is
    always up in mock mode, so calling it unconditionally is safe in
    both modes.

    :param mock_llm_server_url: Mock server URL, or ``None`` in real mode.
    """
    reset_mock_llm(mock_llm_server_url)
    try:
        yield
    finally:
        reset_mock_llm(mock_llm_server_url)


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
    using_mock_llm: bool,  # noqa: F811
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
    :param using_mock_llm: Whether mock LLM mode is active.
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
            f"{mock_llm_server_url}/v1" if using_mock_llm and mock_llm_server_url else None
        ),
    )
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    return JourneySession(agent_name=agent_name, session_id=session_id)
