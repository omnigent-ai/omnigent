"""Unit tests for ``omnigent/model_catalog.py``.

The catalog backs ``sys_list_models`` and the dispatch gate's
canonical→local model-id normalization: provider resolution must mirror
the spawn paths' precedence, and enumeration must hit each provider
kind's real listing endpoint. HTTP is mocked at the transport boundary
(``httpx.MockTransport``) with realistic provider payloads; the
Databricks credential mint is stubbed with the real
:class:`WorkspaceCreds` type.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cachetools import TTLCache

import omnigent.model_catalog as model_catalog
from omnigent.codex_model_vocabulary import codex_spawn_model
from omnigent.model_catalog import (
    ModelEntry,
    ModelListing,
    catalog_for_spec,
    catalog_model_entries,
    list_models_for_worker,
    model_family_token,
    resolve_catalog_model,
    resolve_model_provider,
    spec_harness,
)
from omnigent.model_fallbacks import CODEX_DEFAULT_MODEL, static_model_fallback
from omnigent.model_metadata import (
    ModelCapability,
    ModelCostTier,
    ModelIntent,
    ModelMetadata,
    ModelReasoningMetadata,
    ModelReasoningMode,
    ModelWireAPI,
)
from omnigent.model_resolver import ModelResolutionError, ModelResolutionSource
from omnigent.onboarding.providers import ModelInfo
from omnigent.runtime.credentials.databricks import WorkspaceCreds
from omnigent.spec.types import AgentSpec, ApiKeyAuth, DatabricksAuth, ExecutorSpec


@pytest.fixture(autouse=True)
def _fresh_catalog_cache() -> Iterator[None]:
    """Reset the module TTL cache so tests never replay each other's listings.

    :yields: None.
    """
    model_catalog.clear_model_catalog_cache()
    yield
    model_catalog.clear_model_catalog_cache()


@pytest.fixture(autouse=True)
def _no_ambient_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable ambient credential detection for hermetic resolution.

    Without this, a dev box's env keys / CLI logins leak into the
    no-explicit-default fallthrough and resolution becomes
    machine-dependent.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)


def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, yaml_text: str) -> None:
    """Point the provider config layer at an isolated config file.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir to hold ``config.yaml``.
    :param yaml_text: The config file contents, e.g. a ``providers:`` block.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    (tmp_path / "config.yaml").write_text(yaml_text)


def _worker_spec(harness: str, **executor_kwargs: object) -> AgentSpec:
    """Build a real worker spec declaring *harness*.

    :param harness: The worker harness, e.g. ``"claude-native"``.
    :param executor_kwargs: Extra :class:`ExecutorSpec` fields, e.g.
        ``auth=DatabricksAuth(profile="p")``.
    :returns: An :class:`AgentSpec` shaped like a polly worker.
    """
    return AgentSpec(
        spec_version=1,
        name="worker",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}, **executor_kwargs),  # type: ignore[arg-type]
    )


_DATABRICKS_DEFAULT_CONFIG = (
    "providers:\n  workspace:\n    kind: databricks\n    profile: prof-a\n    default: true\n"
)


# Helper for Unity Catalog model-services fixtures (pi path only).
def _uc_service(name: str, api_types: list[str]) -> dict:
    return {"name": f"model-services/{name}", "supported_api_types": api_types}


# Realistic serving-endpoints page (non-pi harnesses use /api/2.0/serving-endpoints
# which returns databricks-* ids, not system.ai.* ids).
_SERVING_ENDPOINTS_PAGE = {
    "endpoints": [
        {
            "name": "databricks-claude-sonnet-4-6",
            "creator": "system",
            "task": "llm/v1/chat",
            "state": {"ready": "READY"},
        },
        {
            "name": "databricks-gpt-5-4",
            "creator": "system",
            "task": "llm/v1/chat",
            "state": {"ready": "READY"},
        },
        {
            "name": "databricks-meta-llama-3-3-70b-instruct",
            "creator": "system",
            "task": "llm/v1/chat",
            "state": {"ready": "READY"},
        },
        {
            "name": "databricks-bge-large-en",
            "creator": "system",
            "task": "llm/v1/embeddings",
            "state": {"ready": "READY"},
        },
    ]
}


# ── Provider resolution ────────────────────────────────────


def test_resolve_provider_databricks_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A databricks default provider resolves to its profile for every worker.

    If this regressed, the dispatch gate could not localize canonical
    ids for gateway children and ``sys_list_models`` would report no
    gateway source.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(monkeypatch, tmp_path, _DATABRICKS_DEFAULT_CONFIG)
    for harness in ("claude-native", "codex-native", "pi", "pi-native", "native-pi"):
        provider = resolve_model_provider(_worker_spec(harness), harness)
        assert provider.kind == "databricks", f"harness {harness}: {provider}"
        assert provider.profile == "prof-a"


def test_resolve_provider_key_kind_resolves_family_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A key provider resolves base_url + secret for the harness family.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _isolate_config(
        monkeypatch,
        tmp_path,
        "providers:\n"
        "  anthropic:\n"
        "    kind: key\n"
        "    default: true\n"
        "    anthropic:\n"
        "      base_url: https://api.anthropic.com\n"
        "      api_key: $ANTHROPIC_API_KEY\n",
    )
    provider = resolve_model_provider(_worker_spec("claude-native"), "claude-native")
    assert provider.kind == "key"
    assert provider.family == "anthropic"
    assert provider.base_url == "https://api.anthropic.com"
    # The $VAR reference resolved to the real secret — the enumerator
    # authenticates with this value.
    assert provider.api_key == "sk-ant-test"


def test_resolve_provider_subscription_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A subscription default resolves to its CLI (static enumeration).

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(
        monkeypatch,
        tmp_path,
        "providers:\n  claude:\n    kind: subscription\n    cli: claude\n    default: true\n",
    )
    provider = resolve_model_provider(_worker_spec("claude-native"), "claude-native")
    assert provider.kind == "subscription"
    assert provider.cli == "claude"


def test_resolve_provider_cli_config_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cli-config default resolves as usable, never "no credentials".

    The entry's credential lives in the codex CLI's own ``config.toml``
    (an auth command / env key codex runs at launch), so the catalog has
    nothing to resolve statically — falling into the inline-family loop
    used to misreport the worker as having no resolvable credentials.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(
        monkeypatch,
        tmp_path,
        "providers:\n  codex-gateway:\n    kind: cli-config\n    cli: codex\n"
        "    model_provider: Databricks\n    default: true\n",
    )
    provider = resolve_model_provider(_worker_spec("codex-native"), "codex-native")
    assert provider.kind == "cli-config"
    assert provider.cli == "codex"
    assert "codex-gateway" in provider.detail
    assert "Databricks" in provider.detail


@pytest.mark.parametrize("harness", ["cursor", "cursor-native", "native-cursor"])
def test_resolve_provider_cursor_is_cli_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, harness: str
) -> None:
    """Cursor harnesses resolve to cursor-agent's own login, not ``none``.

    cursor-agent has no omnigent-side provider config — it always brings
    its own stored login — so the resolution must not fall through to the
    "harness has no model-provider resolution" dead-worker note.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    :param harness: The cursor harness spelling under test.
    """
    _isolate_config(monkeypatch, tmp_path, "")
    provider = resolve_model_provider(_worker_spec(harness), harness)
    assert provider.kind == "subscription"
    assert provider.cli == "cursor-agent"


def test_resolve_provider_spec_databricks_auth_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit spec ``auth: {type: databricks}`` resolves to that profile.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(monkeypatch, tmp_path, "")
    spec = _worker_spec("claude-native", auth=DatabricksAuth(profile="spec-prof"))
    provider = resolve_model_provider(spec, "claude-native")
    assert provider.kind == "databricks"
    assert provider.profile == "spec-prof"


def test_resolve_provider_spec_api_key_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit ``api_key`` auth resolves to a vendor-direct key provider.

    The claude-family harness maps to the anthropic family with the
    vendor's canonical base URL when the spec names none.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(monkeypatch, tmp_path, "")
    spec = _worker_spec("claude-native", auth=ApiKeyAuth(api_key="sk-ant-inline"))
    provider = resolve_model_provider(spec, "claude-native")
    assert provider.kind == "key"
    assert provider.family == "anthropic"
    assert provider.base_url == "https://api.anthropic.com"
    assert provider.api_key == "sk-ant-inline"


def test_resolve_provider_legacy_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A legacy ``executor.config["profile"]`` resolves to databricks.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(monkeypatch, tmp_path, "")
    spec = AgentSpec(
        spec_version=1,
        name="worker",
        executor=ExecutorSpec(
            type="omnigent", config={"harness": "codex-native", "profile": "legacy-prof"}
        ),
    )
    provider = resolve_model_provider(spec, "codex-native")
    assert provider.kind == "databricks"
    assert provider.profile == "legacy-prof"


def test_resolve_provider_databricks_model_prefix_uses_env_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``databricks-*`` spec model routes via the runner-env profile.

    Mirrors the spawn builders' model-prefix heuristic plus the native
    launch paths' ``DATABRICKS_CONFIG_PROFILE`` fallback.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(monkeypatch, tmp_path, "")
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "env-prof")
    spec = AgentSpec(
        spec_version=1,
        name="worker",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "pi"},
            model="databricks-claude-opus-4-8",
        ),
    )
    provider = resolve_model_provider(spec, "pi")
    assert provider.kind == "databricks"
    assert provider.profile == "env-prof"


@pytest.mark.parametrize(
    ("spec", "harness"),
    [
        # No providers, no auth, no ambient → nothing resolves.
        pytest.param(_worker_spec("claude-native"), "claude-native", id="nothing-configured"),
        # A harness outside the provider-resolution map.
        pytest.param(_worker_spec("unknown-harness"), "unknown-harness", id="unknown-harness"),
        # A structural stub without spec attributes must degrade, not raise.
        pytest.param(
            SimpleNamespace(executor=SimpleNamespace(type="omnigent", config={})),
            "claude-native",
            id="structural-stub",
        ),
    ],
)
def test_resolve_provider_none_cases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    spec: object,
    harness: str,
) -> None:
    """Unresolvable workers come back as kind ``"none"``, never an exception.

    The gate passes models through unchanged on ``"none"`` and the tool
    reports the row as a dead worker — a raise here would crash both.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    :param spec: The worker spec under test.
    :param harness: The worker harness under test.
    """
    _isolate_config(monkeypatch, tmp_path, "")
    provider = resolve_model_provider(spec, harness)
    assert provider.kind == "none"
    # The detail is what surfaces in the tool's note — it must say why.
    assert provider.detail != ""


# ── Per-harness legacy auth parity with the spawn-env builders ─────


@pytest.mark.parametrize("harness", ["codex-native", "pi"])
@pytest.mark.parametrize(
    "executor_kwargs",
    [
        pytest.param({"auth": ApiKeyAuth(api_key="sk-unconsumed")}, id="api-key-auth"),
        pytest.param({"auth": DatabricksAuth(profile="unconsumed-prof")}, id="databricks-auth"),
        pytest.param({"profile": "top-level-prof"}, id="top-level-executor-profile"),
    ],
)
def test_resolve_provider_codex_pi_ignore_legacy_auth_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    harness: str,
    executor_kwargs: dict[str, object],
) -> None:
    """codex / pi report ``none`` for legacy auth fields their builders skip.

    ``_build_codex_spawn_env`` / ``_build_pi_spawn_env`` consume ONLY
    ``config["profile"]`` and the ``databricks-*`` model prefix — never
    ``auth:`` blocks or top-level ``executor.profile``. If the catalog
    resolved these fields anyway, ``sys_list_models`` would advertise
    models the spawned child has no credentials to reach (the resolution
    parity gap).

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    :param harness: The worker harness under test.
    :param executor_kwargs: The legacy auth field the builder ignores.
    """
    _isolate_config(monkeypatch, tmp_path, "")
    provider = resolve_model_provider(_worker_spec(harness, **executor_kwargs), harness)
    assert provider.kind == "none", f"{harness} must not consume {executor_kwargs}: {provider}"
    # The note must steer the author to the config shape the spawn path
    # actually reads, not just say "unconfigured".
    assert "does not consume legacy auth" in provider.detail
    assert "providers" in provider.detail


def test_resolve_provider_claude_sdk_consumes_top_level_executor_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """claude-sdk DOES consume the deprecated top-level ``executor.profile``.

    The contrast case to the codex/pi test above:
    ``_build_claude_sdk_spawn_env`` reads ``config["profile"] or
    executor.profile``, so the catalog must resolve it too — reporting
    ``none`` here would mark a perfectly routable claude worker dead.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(monkeypatch, tmp_path, "")
    spec = _worker_spec("claude-native", profile="top-level-prof")
    provider = resolve_model_provider(spec, "claude-native")
    assert provider.kind == "databricks"
    assert provider.profile == "top-level-prof"


def test_resolve_provider_openai_agents_api_key_auth_keeps_base_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """openai-agents resolves spec ``api_key`` auth with its base_url.

    ``_build_openai_agents_sdk_spawn_env`` threads ``auth.base_url``
    into ``HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL``, so the listing must
    enumerate that gateway — falling back to the vendor default would
    list api.openai.com models the child never talks to.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(monkeypatch, tmp_path, "")
    spec = _worker_spec(
        "openai-agents",
        auth=ApiKeyAuth(api_key="sk-oai-inline", base_url="https://gw.example.com/v1"),
    )
    provider = resolve_model_provider(spec, "openai-agents")
    assert provider.kind == "key"
    assert provider.family == "openai"
    assert provider.base_url == "https://gw.example.com/v1"
    assert provider.api_key == "sk-oai-inline"


def test_resolve_provider_global_auth_consumed_by_claude_sdk_not_codex(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A global ``auth:`` api_key block routes claude-sdk but not codex.

    ``_build_claude_sdk_spawn_env`` falls through to
    ``_load_global_auth()`` (→ ``apiKeyHelper``); the codex builder
    never reads the global block. One config, two verdicts — collapsing
    them either way mis-advertises one of the workers.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(monkeypatch, tmp_path, "auth:\n  type: api_key\n  api_key: sk-global\n")
    claude = resolve_model_provider(_worker_spec("claude-native"), "claude-native")
    # claude-sdk consumes the global key via apiKeyHelper → vendor API.
    assert claude.kind == "key"
    assert claude.family == "anthropic"
    assert claude.api_key == "sk-global"
    codex = resolve_model_provider(_worker_spec("codex-native"), "codex-native")
    # The codex spawn path never reads the global auth: block.
    assert codex.kind == "none"


# ── Enumeration per provider kind ──────────────────────────


def _databricks_transport(
    requests_seen: list[httpx.Request],
) -> httpx.MockTransport:
    """Build a transport serving the realistic serving-endpoints page.

    :param requests_seen: Mutable list capturing each request for
        header/path assertions.
    :returns: The mock transport.
    """

    _UC_PAGE = {
        "model_services": [
            _uc_service("system.ai.claude-sonnet-4-6", ["anthropic/v1/messages"]),
            _uc_service(
                "system.ai.gpt-5-4", ["mlflow/v1/chat/completions", "openai/v1/responses"]
            ),
            _uc_service("system.ai.gpt-responses-only", ["openai/v1/responses"]),
            _uc_service("system.ai.meta-llama-3-3-70b-instruct", ["mlflow/v1/chat/completions"]),
            _uc_service("system.ai.qwen3-embedding", ["mlflow/v1/embeddings"]),
        ]
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        """Serve serving-endpoints (non-pi) or UC model-services (pi)."""
        requests_seen.append(request)
        if request.url.path == "/api/2.0/serving-endpoints":
            return httpx.Response(200, json=_SERVING_ENDPOINTS_PAGE)
        if request.url.path == "/api/2.1/unity-catalog/model-services":
            return httpx.Response(200, json=_UC_PAGE)
        return httpx.Response(404, json={"error": str(request.url)})

    return httpx.MockTransport(_handler)


def _stub_workspace_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the Databricks credential mint with real ``WorkspaceCreds``.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        model_catalog,
        "resolve_databricks_workspace",
        lambda profile: WorkspaceCreds(host="https://workspace.example.com", token="dapi-test"),
    )


def test_databricks_listing_filters_to_llm_wire_surfaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gateway listing keeps supported LLM wires and tags families.

    The embeddings endpoint must be excluded — including it would let an
    orchestrator dispatch a worker onto a non-chat endpoint.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(monkeypatch, tmp_path, _DATABRICKS_DEFAULT_CONFIG)
    _stub_workspace_creds(monkeypatch)
    requests_seen: list[httpx.Request] = []

    listing = list_models_for_worker(
        _worker_spec("pi"), "pi", transport=_databricks_transport(requests_seen)
    )

    assert listing.source == "gateway"
    assert listing.verified is True
    # The profile's minted token authenticated the listing call.
    assert requests_seen[0].headers["authorization"] == "Bearer dapi-test"
    by_id = {m.id: m for m in listing.models}
    # Pi uses UC model-services API → system.ai.* ids; embeddings endpoint excluded.
    assert set(by_id) == {
        "system.ai.claude-sonnet-4-6",
        "system.ai.gpt-5-4",
        "system.ai.gpt-responses-only",
        "system.ai.meta-llama-3-3-70b-instruct",
    }
    assert by_id["system.ai.claude-sonnet-4-6"].family == "claude"
    assert by_id["system.ai.gpt-5-4"].family == "openai"
    assert by_id["system.ai.meta-llama-3-3-70b-instruct"].family == "other"
    assert by_id["system.ai.claude-sonnet-4-6"].metadata.wire_apis == frozenset(
        {ModelWireAPI.ANTHROPIC_MESSAGES}
    )
    assert by_id["system.ai.gpt-5-4"].metadata.wire_apis == frozenset(
        {ModelWireAPI.OPENAI_CHAT, ModelWireAPI.OPENAI_RESPONSES}
    )
    assert by_id["system.ai.gpt-responses-only"].metadata.wire_apis == frozenset(
        {ModelWireAPI.OPENAI_RESPONSES}
    )


def test_databricks_listing_skips_explicitly_non_ready_endpoints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Endpoints whose state says not-ready are skipped; absent state stays.

    Listing a provisioning/failed endpoint would let an orchestrator
    dispatch a worker onto an endpoint that immediately errors. But the
    serving-endpoints API may omit ``state`` entirely, so dropping
    absent-state endpoints would hide every model on those workspaces —
    only an EXPLICIT non-READY value excludes.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(monkeypatch, tmp_path, _DATABRICKS_DEFAULT_CONFIG)
    _stub_workspace_creds(monkeypatch)
    # Pi uses the UC model-services API — all listed services are available
    # (UC has no per-service ready/not-ready flag).
    page = {
        "model_services": [
            _uc_service("system.ai.claude-ready", ["mlflow/v1/chat/completions"]),
            _uc_service("system.ai.claude-stateless", ["mlflow/v1/chat/completions"]),
        ]
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        """Serve the UC model-services page for pi."""
        return httpx.Response(200, json=page)

    listing = list_models_for_worker(
        _worker_spec("pi"), "pi", transport=httpx.MockTransport(_handler)
    )

    assert {m.id for m in listing.models} == {
        "system.ai.claude-ready",
        "system.ai.claude-stateless",
    }


@pytest.mark.parametrize(
    ("harness", "expected_ids"),
    [
        pytest.param("claude-native", {"databricks-claude-sonnet-4-6"}, id="claude-family-only"),
        pytest.param("codex-native", {"databricks-gpt-5-4"}, id="openai-family-only"),
        # The executor-type spelling spec_harness() yields when a spec
        # declares no config harness must filter like its canonical
        # sibling — an unrecognized spelling silently disables the
        # filter and lists wrong-family models.
        pytest.param(
            "claude_sdk", {"databricks-claude-sonnet-4-6"}, id="claude-sdk-executor-type"
        ),
        pytest.param("claude-sdk", {"databricks-claude-sonnet-4-6"}, id="claude-sdk-spelling"),
        pytest.param("codex", {"databricks-gpt-5-4"}, id="codex-spelling"),
        # openai-agents / openai-agents-sdk / agents_sdk outcomes are
        # deliberately NOT pinned here: a later change relaxes that harness to
        # multi-model (any validated id), flipping the expected set.
        pytest.param(
            "pi",
            # Pi uses UC model-services API → system.ai.* ids (embeddings excluded).
            {
                "system.ai.claude-sonnet-4-6",
                "system.ai.gpt-5-4",
                "system.ai.gpt-responses-only",
                "system.ai.meta-llama-3-3-70b-instruct",
            },
            id="pi-everything",
        ),
    ],
)
def test_family_filter_per_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    harness: str,
    expected_ids: set[str],
) -> None:
    """Each worker's list is filtered to the family its harness can run.

    This reuses the family rule: an id that survives the filter is
    exactly an id the dispatch gate would accept for that worker.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    :param harness: The worker harness under test.
    :param expected_ids: The ids that must survive the filter.
    """
    _isolate_config(monkeypatch, tmp_path, _DATABRICKS_DEFAULT_CONFIG)
    _stub_workspace_creds(monkeypatch)
    requests_seen: list[httpx.Request] = []

    listing = list_models_for_worker(
        _worker_spec(harness), harness, transport=_databricks_transport(requests_seen)
    )

    assert {m.id for m in listing.models} == expected_ids


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("databricks-claude-opus-4-8", "claude"),
        ("claude-sonnet-5", "claude"),
        ("databricks-gpt-5-5", "openai"),
        ("gpt-5.1-codex", "openai"),
        # GLM / Kimi are codex-runnable, so they carry the codex-compatible
        # token — a candidate filter keyed on it must not strip them.
        ("glm-5-2", "openai"),
        ("databricks-glm-5-2", "openai"),
        ("system.ai.glm-5-2", "openai"),
        ("kimi-k2", "openai"),
        ("databricks-kimi-k2-6", "openai"),
        ("system.ai.kimi-k2-instruct", "openai"),
        ("kimi-for-coding", "openai"),
        ("databricks-meta-llama-3.3-70b-instruct", "other"),
        ("gemini-3.5-flash", "other"),
        # Segment matching, not substring: an unrelated endpoint name that
        # happens to contain the letters is not the GLM family.
        ("glmqlfit-eval", "other"),
    ],
)
def test_model_family_token_tags_codex_compatible_families(model_id: str, expected: str) -> None:
    """The family token names the harness family that can serve the id.

    :param model_id: Model id under test.
    :param expected: The family token it must carry.
    """
    assert model_family_token(model_id) == expected


def test_codex_worker_listing_keeps_glm_and_kimi_endpoints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A codex worker's list keeps the GLM/Kimi endpoints it can serve.

    The workspace serves these over the same Responses wire codex speaks,
    so filtering them out would hide runnable models from the router while
    the dispatch gate would have accepted them.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(monkeypatch, tmp_path, _DATABRICKS_DEFAULT_CONFIG)
    _stub_workspace_creds(monkeypatch)
    page = {
        "endpoints": [
            {
                "name": name,
                "creator": "system",
                "task": "llm/v1/chat",
                "state": {"ready": "READY"},
            }
            for name in (
                "databricks-gpt-5-5",
                "databricks-glm-5-2",
                "databricks-kimi-k2-6",
                "databricks-claude-sonnet-4-6",
                "databricks-meta-llama-3-3-70b-instruct",
            )
        ]
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        """Serve the serving-endpoints page for the codex worker."""
        return httpx.Response(200, json=page)

    listing = list_models_for_worker(
        _worker_spec("codex-native"), "codex-native", transport=httpx.MockTransport(_handler)
    )

    assert {m.id for m in listing.models} == {
        "databricks-gpt-5-5",
        "databricks-glm-5-2",
        "databricks-kimi-k2-6",
    }


def test_openai_compatible_listing_maps_ids_and_context_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An OpenRouter-style ``/v1/models`` page maps ids + context windows.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _isolate_config(
        monkeypatch,
        tmp_path,
        "providers:\n"
        "  openrouter:\n"
        "    kind: gateway\n"
        "    default: true\n"
        "    openai:\n"
        "      base_url: https://openrouter.ai/api/v1\n"
        "      api_key: $OPENROUTER_API_KEY\n"
        "      wire_api: chat\n",
    )
    requests_seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Serve a realistic OpenRouter models page."""
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "openai/gpt-5.4",
                        "name": "OpenAI: GPT-5.4",
                        "context_length": 400000,
                        "pricing": {"prompt": "0.000002", "completion": "0.00001"},
                    },
                    {
                        "id": "moonshotai/kimi-k2.6",
                        "name": "Kimi K2.6",
                        "context_length": 262144,
                        "pricing": {"prompt": "0.0000005", "completion": "0.000002"},
                    },
                ]
            },
        )

    listing = list_models_for_worker(
        _worker_spec("codex-native"), "codex-native", transport=httpx.MockTransport(_handler)
    )

    # The base URL already ends in /v1, so the listing URL appends /models.
    assert str(requests_seen[0].url) == "https://openrouter.ai/api/v1/models"
    assert requests_seen[0].headers["authorization"] == "Bearer sk-or-test"
    assert listing.source == "openai-compatible"
    assert listing.verified is True
    # codex-native keeps the codex-compatible ids (GPT plus the Kimi family
    # it serves over the same wire); the provider-reported context window
    # rides along.
    assert [(m.id, m.context_window) for m in listing.models] == [
        ("openai/gpt-5.4", 400000),
        ("moonshotai/kimi-k2.6", 262144),
    ]


def test_anthropic_api_listing_uses_api_key_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real Anthropic key enumerates via ``/v1/models`` with vendor headers.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _isolate_config(
        monkeypatch,
        tmp_path,
        "providers:\n"
        "  anthropic:\n"
        "    kind: key\n"
        "    default: true\n"
        "    anthropic:\n"
        "      base_url: https://api.anthropic.com\n"
        "      api_key: $ANTHROPIC_API_KEY\n",
    )
    requests_seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Serve a realistic Anthropic models page."""
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "type": "model",
                        "id": "claude-opus-4-8",
                        "display_name": "Claude Opus 4.8",
                        "max_input_tokens": 1_000_000,
                        "capabilities": {
                            "thinking": {
                                "supported": True,
                                "types": {
                                    "enabled": {"supported": False},
                                    "adaptive": {"supported": True},
                                },
                            },
                            "effort": {
                                "supported": True,
                                "low": {"supported": True},
                                "medium": {"supported": True},
                                "high": {"supported": True},
                                "xhigh": {"supported": True},
                                "max": {"supported": True},
                            },
                        },
                    },
                    {
                        "type": "model",
                        "id": "claude-sonnet-4-6",
                        "display_name": "Claude Sonnet 4.6",
                    },
                ],
                "has_more": False,
            },
        )

    listing = list_models_for_worker(
        _worker_spec("claude-native"), "claude-native", transport=httpx.MockTransport(_handler)
    )

    assert str(requests_seen[0].url) == "https://api.anthropic.com/v1/models"
    # The Anthropic API authenticates via x-api-key + anthropic-version,
    # NOT a bearer header — a Bearer here means the wrong fetcher ran.
    assert requests_seen[0].headers["x-api-key"] == "sk-ant-test"
    assert requests_seen[0].headers["anthropic-version"] == "2023-06-01"
    assert listing.source == "anthropic-api"
    assert [m.id for m in listing.models] == ["claude-opus-4-8", "claude-sonnet-4-6"]
    opus = listing.models[0]
    assert opus.context_window == 1_000_000
    assert opus.metadata.supports(ModelCapability.REASONING) is True
    assert opus.metadata.reasoning is not None
    assert opus.metadata.reasoning.modes == frozenset({ModelReasoningMode.ADAPTIVE})


def test_subscription_listing_is_static_and_unverified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A subscription CLI yields the curated static list, ``verified=False``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(
        monkeypatch,
        tmp_path,
        "providers:\n  claude:\n    kind: subscription\n    cli: claude\n    default: true\n",
    )
    listing = list_models_for_worker(_worker_spec("claude-native"), "claude-native")
    assert listing.source == "static"
    assert listing.verified is False
    # Exactly the curated claude tiers — these are aliases, not a live list.
    assert [m.id for m in listing.models] == [
        "claude-fable-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ]
    assert "CLI login" in listing.note
    assert listing.static_fallback is not None
    assert listing.static_fallback.owner == "Claude subscription adapter"
    payload = model_catalog._listing_payload(listing)
    assert payload["static_fallback"] == {
        "owner": "Claude subscription adapter",
        "provenance": "Omnigent's release-curated Claude Code alias catalog",
        "discovery_gap": "Claude subscription logins expose no model-listing API",
    }


def test_cli_config_listing_is_static_and_unverified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cli-config provider yields the codex curated list, not a dead row.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(
        monkeypatch,
        tmp_path,
        "providers:\n  codex-gateway:\n    kind: cli-config\n    cli: codex\n"
        "    model_provider: Databricks\n    default: true\n",
    )
    listing = list_models_for_worker(_worker_spec("codex-native"), "codex-native")
    assert listing.source == "static"
    assert listing.verified is False
    assert [m.id for m in listing.models] == [
        "gpt-5.6-sol",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.5",
    ]
    # The note must say the CLI resolves the credential itself — this row
    # is a working worker, not a credentials preflight failure.
    assert "resolved by the CLI at launch" in listing.note
    assert "cannot run here" not in listing.note
    assert listing.static_fallback is not None
    assert listing.static_fallback.owner == "Codex CLI-config adapter"


@pytest.mark.parametrize(
    ("provider_kind", "cli"),
    [
        ("subscription", "claude"),
        ("subscription", "codex"),
        ("cli-config", "codex"),
    ],
)
def test_static_model_fallbacks_document_ownership(
    provider_kind: str,
    cli: str,
) -> None:
    """Every registered fallback explains who owns it and why it exists."""
    fallback = static_model_fallback(provider_kind, cli)

    assert fallback is not None
    assert fallback.model_ids
    assert fallback.owner
    assert fallback.provenance
    assert fallback.discovery_gap


@pytest.mark.parametrize("provider_kind", ["subscription", "cli-config"])
def test_codex_catalog_ids_are_spelled_the_way_codex_accepts(provider_kind: str) -> None:
    """Codex's catalogs carry its dotted slugs, not the hyphenated serving ids.

    Codex's own backend 400s on ``gpt-5-6-sol``; only ``gpt-5.6-sol`` reaches a
    ChatGPT-account login. The two spellings still compare equal, so a routed
    arm keeps matching either way.

    :param provider_kind: The registered provider kind under test.
    """
    fallback = static_model_fallback(provider_kind, "codex")
    assert fallback is not None
    for model_id in fallback.model_ids:
        assert not model_id.startswith("databricks-"), model_id
        assert codex_spawn_model(model_id) == model_id, (
            f"{model_id!r} is not codex's own spelling for itself"
        )


def test_codex_default_model_names_a_concrete_variant() -> None:
    """The codex launch default is a tiered model, not a bare family alias.

    The bundled OpenAI catalog's newest row is ``gpt-5.6``, which codex rejects
    as a family name; a default must name a variant its backend serves.
    """
    fallback = static_model_fallback("subscription", "codex")
    assert fallback is not None
    assert CODEX_DEFAULT_MODEL in fallback.model_ids
    assert codex_spawn_model(CODEX_DEFAULT_MODEL) == CODEX_DEFAULT_MODEL
    # A bare family alias has no tier segment after the dotted version.
    assert re.fullmatch(r"gpt-\d+\.\d+", CODEX_DEFAULT_MODEL) is None


def test_cursor_listing_uses_live_cli_base_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cursor worker lists base models discovered from cursor-agent.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    from omnigent import cursor_native

    _isolate_config(monkeypatch, tmp_path, "")
    monkeypatch.setattr(
        cursor_native,
        "list_cursor_cli_model_options",
        lambda: [
            {
                "id": "provider-latest",
                "displayName": "Provider Latest",
                "isDefault": True,
                "isCurrent": False,
            }
        ],
    )
    listing = list_models_for_worker(_worker_spec("cursor-native"), "cursor-native")
    assert listing.source == "cli"
    assert listing.verified is True
    assert [m.id for m in listing.models] == ["provider-latest"]
    assert "live models advertised" in listing.note


def test_cursor_listing_failure_is_empty_and_retryable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transient Cursor CLI failure does not cache an empty catalog."""
    from omnigent import cursor_native

    _isolate_config(monkeypatch, tmp_path, "")
    calls = 0

    def fail() -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        raise OSError("cursor unavailable")

    monkeypatch.setattr(cursor_native, "list_cursor_cli_model_options", fail)

    first = list_models_for_worker(_worker_spec("cursor-native"), "cursor-native")
    second = list_models_for_worker(_worker_spec("cursor-native"), "cursor-native")

    assert first.source == second.source == "none"
    assert first.models == second.models == ()
    assert calls == 2


def test_none_listing_explains_dead_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unresolvable provider yields an empty list with a preflight note.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(monkeypatch, tmp_path, "")
    listing = list_models_for_worker(_worker_spec("claude-native"), "claude-native")
    assert listing.source == "none"
    assert listing.models == ()
    # The note is the dead-worker preflight signal the orchestrator reads.
    assert "cannot run here" in listing.note


# ── TTL cache + failure behavior ───────────────────────────


def test_listing_cached_within_ttl_and_refetched_after_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Repeat enumerations replay from the TTL cache until expiry.

    A fan-out turn calls the tool/gate repeatedly; without the cache
    every call would re-hit the provider API. The fake timer proves the
    fetch is wired THROUGH the cache (count stays 1) and that expiry
    re-fetches (count becomes 2).

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(monkeypatch, tmp_path, _DATABRICKS_DEFAULT_CONFIG)
    _stub_workspace_creds(monkeypatch)
    now = {"t": 0.0}
    monkeypatch.setattr(
        model_catalog, "_listing_cache", TTLCache(maxsize=64, ttl=300.0, timer=lambda: now["t"])
    )
    requests_seen: list[httpx.Request] = []
    transport = _databricks_transport(requests_seen)

    first = list_models_for_worker(_worker_spec("pi"), "pi", transport=transport)
    second = list_models_for_worker(_worker_spec("pi"), "pi", transport=transport)
    # One fetch served both calls — the second replayed from the cache.
    assert len(requests_seen) == 1
    assert [m.id for m in second.models] == [m.id for m in first.models]

    now["t"] = 301.0
    list_models_for_worker(_worker_spec("pi"), "pi", transport=transport)
    # Past the TTL the cache entry expired and the provider was re-hit.
    assert len(requests_seen) == 2


def test_listing_failure_reported_and_not_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed fetch reports source ``none`` and retries on the next call.

    Caching a failure would pin a transient provider outage for the full
    TTL; the second call here must succeed once the endpoint recovers.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(monkeypatch, tmp_path, _DATABRICKS_DEFAULT_CONFIG)
    _stub_workspace_creds(monkeypatch)
    calls = {"n": 0}

    def _flaky_handler(request: httpx.Request) -> httpx.Response:
        """Fail the first listing call, succeed afterwards."""
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return httpx.Response(200, json=_SERVING_ENDPOINTS_PAGE)

    # Use codex-native (serving-endpoints path) to test generic failure/retry logic.
    transport = httpx.MockTransport(_flaky_handler)
    failed = list_models_for_worker(
        _worker_spec("codex-native"), "codex-native", transport=transport
    )
    assert failed.source == "none"
    assert failed.models == ()
    # The note names the failure so the orchestrator can report it.
    assert "enumeration failed" in failed.note

    recovered = list_models_for_worker(
        _worker_spec("codex-native"), "codex-native", transport=transport
    )
    # Recovery proves the failure was NOT cached for the TTL window.
    assert recovered.source == "gateway"
    # codex-native filters to openai-family only → 1 model from _SERVING_ENDPOINTS_PAGE
    assert len(recovered.models) == 1


def test_listing_cache_is_keyed_by_credential_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same provider coordinates + different credential ⇒ separate entries.

    Two tenants sharing kind + base_url but holding different api keys
    must never replay each other's listing through the runner-wide TTL
    cache (the cross-tenant cache-replay).
    The handler encodes the presented bearer into the model id, so a
    cross-credential replay is directly visible in the listing content.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    monkeypatch.setenv("GW_LISTING_KEY", "sk-tenant-a")
    _isolate_config(
        monkeypatch,
        tmp_path,
        "providers:\n"
        "  shared-gw:\n"
        "    kind: gateway\n"
        "    default: true\n"
        "    openai:\n"
        "      base_url: https://gw.example.com/v1\n"
        "      api_key: $GW_LISTING_KEY\n"
        "      wire_api: chat\n",
    )
    requests_seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Serve a listing whose model id names the presented bearer."""
        requests_seen.append(request)
        tenant = request.headers["authorization"].removeprefix("Bearer sk-")
        return httpx.Response(200, json={"data": [{"id": f"model-for-{tenant}"}]})

    transport = httpx.MockTransport(_handler)
    spec = _worker_spec("pi")

    first = list_models_for_worker(spec, "pi", transport=transport)
    assert [m.id for m in first.models] == ["model-for-tenant-a"]

    monkeypatch.setenv("GW_LISTING_KEY", "sk-tenant-b")
    second = list_models_for_worker(spec, "pi", transport=transport)
    # Tenant B got a fresh fetch with ITS credential. Replaying tenant
    # A's cached listing here (count 1 / model-for-tenant-a) is exactly
    # the cross-tenant leak: the cache key ignored credential identity.
    assert [m.id for m in second.models] == ["model-for-tenant-b"]
    assert len(requests_seen) == 2

    monkeypatch.setenv("GW_LISTING_KEY", "sk-tenant-a")
    third = list_models_for_worker(spec, "pi", transport=transport)
    # Both per-credential entries coexist: tenant A replays from its own
    # still-fresh entry without a third fetch. A count of 3 would mean
    # the keys collide and each switch evicts the other tenant.
    assert [m.id for m in third.models] == ["model-for-tenant-a"]
    assert len(requests_seen) == 2


def test_failed_auth_command_note_never_leaks_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ``auth_command`` failure surfaces a category, never the command.

    ``subprocess.CalledProcessError`` stringifies the full ``/bin/sh``
    argv — including the ``auth_command`` and any secret embedded in
    it — and failure notes flow into the LLM-visible,
    transcript-persisted ``sys_list_models`` payload. A leak here writes
    the secret into the conversation store.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    secret = "sk-embedded-secret-do-not-leak"
    _isolate_config(
        monkeypatch,
        tmp_path,
        "providers:\n"
        "  leaky:\n"
        "    kind: gateway\n"
        "    default: true\n"
        "    openai:\n"
        "      base_url: https://gw.example.com/v1\n"
        f"      auth_command: printf %s {secret} && exit 1\n"
        "      wire_api: chat\n",
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        """Fail the test if the listing is fetched despite the dead mint."""
        raise AssertionError("the listing must never be fetched when auth minting fails")

    parent = AgentSpec(
        spec_version=1,
        name="orchestrator",
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi"}),
        sub_agents=[_worker_spec("pi")],
    )
    catalog = catalog_for_spec(parent, transport=httpx.MockTransport(_handler))

    # The note names the redacted category so the orchestrator can react…
    assert catalog["worker"]["source"] == "none"
    assert "provider auth command failed" in catalog["worker"]["note"]
    # …and the secret embedded in the failing command never reaches ANY
    # part of the serialized tool payload. If this fails, raw exception
    # text (str(CalledProcessError) quotes the argv) leaked into a note.
    assert secret not in json.dumps(catalog)


# ── catalog_for_spec (the tool payload) ────────────────────


def test_catalog_isolates_per_worker_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One worker's broken provider never hides the other workers' rows.

    The claude worker resolves a subscription (static, no HTTP) while
    the codex worker's gateway listing 503s — the codex row must degrade
    to ``none`` with the failure note while claude and self stay intact.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(
        monkeypatch,
        tmp_path,
        "providers:\n"
        "  claude:\n"
        "    kind: subscription\n"
        "    cli: claude\n"
        "    default: anthropic\n"
        "  workspace:\n"
        "    kind: databricks\n"
        "    profile: prof-a\n"
        "    default: openai\n",
    )
    _stub_workspace_creds(monkeypatch)
    parent = AgentSpec(
        spec_version=1,
        name="orchestrator",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        sub_agents=[
            _worker_spec("claude-native"),
            AgentSpec(
                spec_version=1,
                name="codex",
                executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
            ),
        ],
    )

    def _broken_handler(request: httpx.Request) -> httpx.Response:
        """Fail every gateway listing call."""
        return httpx.Response(503, json={"error": "down"})

    catalog = catalog_for_spec(parent, transport=httpx.MockTransport(_broken_handler))

    assert set(catalog) == {"worker", "codex", "self"}
    # The subscription rows (claude worker + the claude-sdk brain) are
    # unaffected by the gateway outage.
    assert catalog["worker"]["source"] == "static"
    assert next(m["id"] for m in catalog["worker"]["models"]) == "claude-fable-5"
    assert catalog["self"]["source"] == "static"
    # The broken worker degrades informatively instead of crashing the tool.
    assert catalog["codex"]["source"] == "none"
    assert catalog["codex"]["models"] == []
    assert "enumeration failed" in catalog["codex"]["note"]


def test_catalog_payload_is_json_serializable_and_omits_unknown_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The payload round-trips through JSON; ``context_window`` only when known.

    The serving-endpoints API reports no context windows, so gateway
    entries must omit the key rather than carry a fabricated value.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(monkeypatch, tmp_path, _DATABRICKS_DEFAULT_CONFIG)
    _stub_workspace_creds(monkeypatch)
    requests_seen: list[httpx.Request] = []
    parent = AgentSpec(
        spec_version=1,
        name="orchestrator",
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi"}),
        sub_agents=[_worker_spec("pi")],
    )

    catalog = catalog_for_spec(parent, transport=_databricks_transport(requests_seen))
    payload = json.loads(json.dumps(catalog))
    assert payload["worker"]["source"] == "gateway"
    assert all("context_window" not in m for m in payload["worker"]["models"])


def test_catalog_payload_serializes_normalized_model_metadata() -> None:
    """Known metadata is exposed without inventing values for unknown fields."""
    listing = ModelListing(
        source="catalog",
        verified=True,
        models=(
            ModelEntry(
                id="provider/model-a",
                family="provider-family",
                metadata=ModelMetadata(
                    supported_capabilities=frozenset({ModelCapability.TOOL_USE}),
                    unsupported_capabilities=frozenset({ModelCapability.VISION}),
                    context_window=200_000,
                    max_output_tokens=16_000,
                    cost_tier=ModelCostTier.STANDARD,
                    wire_apis=frozenset({ModelWireAPI.OPENAI_RESPONSES}),
                    reasoning=ModelReasoningMetadata(
                        modes=frozenset({ModelReasoningMode.ADAPTIVE}),
                        efforts=frozenset({"low", "high"}),
                    ),
                ),
            ),
        ),
        note="test catalog",
    )

    payload = model_catalog._listing_payload(listing)

    assert payload["models"] == [
        {
            "id": "provider/model-a",
            "family": "provider-family",
            "context_window": 200_000,
            "max_output_tokens": 16_000,
            "capabilities": {"tool-use": True, "vision": False},
            "cost_tier": "standard",
            "wire_apis": ["openai-responses"],
            "reasoning": {"modes": ["adaptive"], "efforts": ["high", "low"]},
        }
    ]


def test_bundled_catalog_entries_normalize_capabilities_context_and_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MLflow model facts become provider-neutral resolver metadata."""
    models = [
        ModelInfo(
            name="provider/model-premium",
            provider="provider",
            mode="chat",
            supports_function_calling=True,
            supports_reasoning=True,
            supports_vision=False,
            supports_structured_output=True,
            max_input_tokens=100_000,
            max_output_tokens=20_000,
            input_price=10.0,
            output_price=30.0,
        ),
        ModelInfo(
            name="provider/model-economy",
            provider="provider",
            mode="chat",
            input_price=0.1,
            output_price=0.2,
        ),
        ModelInfo(
            name="provider/model-standard",
            provider="provider",
            mode="chat",
            input_price=2.0,
            output_price=6.0,
        ),
    ]
    monkeypatch.setattr("omnigent.onboarding.providers.get_chat_models", lambda _provider: models)

    entries = catalog_model_entries("provider")

    premium = entries[0]
    assert premium.metadata.context_window == 100_000
    assert premium.metadata.max_output_tokens == 20_000
    assert premium.metadata.cost_tier == ModelCostTier.PREMIUM
    assert premium.metadata.supports(ModelCapability.TOOL_USE) is True
    assert premium.metadata.supports(ModelCapability.REASONING) is True
    assert premium.metadata.supports(ModelCapability.VISION) is False
    assert premium.metadata.supports(ModelCapability.STRUCTURED_OUTPUT) is True
    assert entries[1].metadata.cost_tier == ModelCostTier.ECONOMY
    assert entries[2].metadata.cost_tier == ModelCostTier.STANDARD


def test_resolve_catalog_model_uses_intent_and_configured_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = [
        ModelInfo(
            name="model-economy",
            provider="provider",
            mode="chat",
            input_price=0.1,
            output_price=0.2,
        ),
        ModelInfo(
            name="model-premium",
            provider="provider",
            mode="chat",
            input_price=10.0,
            output_price=30.0,
        ),
    ]
    monkeypatch.setattr("omnigent.onboarding.providers.get_chat_models", lambda _provider: models)

    powerful = resolve_catalog_model("provider", intent=ModelIntent.POWERFUL)
    configured = resolve_catalog_model(
        "provider",
        intent=ModelIntent.POWERFUL,
        configured_default="model-configured",
        family="other",
    )

    assert powerful.model_id == "model-premium"
    assert powerful.source == ModelResolutionSource.LIVE_CATALOG
    assert configured.model_id == "model-configured"
    assert configured.source == ModelResolutionSource.CONFIGURED_DEFAULT


def test_resolve_catalog_default_preserves_provider_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default resolution skips specialty models after family filtering."""
    models = [
        ModelInfo(name="gpt-audio-new", provider="gateway", mode="chat"),
        ModelInfo(name="gpt-general", provider="gateway", mode="chat"),
        ModelInfo(name="claude-general", provider="gateway", mode="chat"),
    ]
    monkeypatch.setattr("omnigent.onboarding.providers.get_chat_models", lambda provider: models)

    resolution = resolve_catalog_model("gateway", family="openai")

    assert resolution.model_id == "gpt-general"
    assert resolution.source == ModelResolutionSource.CONFIGURED_DEFAULT


def test_resolve_catalog_default_preserves_provider_tier_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default resolution retains broadly accessible provider tiers."""
    monkeypatch.setattr(
        "omnigent.onboarding.providers.get_chat_models",
        lambda _provider: [
            ModelInfo(name="claude-opus-new", provider="anthropic", mode="chat"),
            ModelInfo(name="claude-sonnet-stable", provider="anthropic", mode="chat"),
        ],
    )

    resolution = resolve_catalog_model("anthropic", family="claude")

    assert resolution.model_id == "claude-sonnet-stable"
    assert resolution.source == ModelResolutionSource.CONFIGURED_DEFAULT


@pytest.mark.parametrize(
    ("family", "expected_model"),
    [
        ("claude", "databricks-claude-general"),
        ("openai", "databricks-gpt-general"),
    ],
)
def test_resolve_databricks_default_requires_gateway_routable_id(
    monkeypatch: pytest.MonkeyPatch,
    family: str,
    expected_model: str,
) -> None:
    """Databricks defaults exclude bare vendor ids the gateway rejects."""
    models = [
        ModelInfo(name="claude-newer", provider="databricks", mode="chat"),
        ModelInfo(name="gpt-newer", provider="databricks", mode="chat"),
        ModelInfo(name="databricks-claude-general", provider="databricks", mode="chat"),
        ModelInfo(name="databricks-gpt-general", provider="databricks", mode="chat"),
    ]
    monkeypatch.setattr("omnigent.onboarding.providers.get_chat_models", lambda _provider: models)

    resolution = resolve_catalog_model("databricks", family=family)

    assert resolution.model_id == expected_model
    assert resolution.model_id.startswith("databricks-")
    assert resolution.family == family


def test_resolve_catalog_model_fails_when_discovery_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.onboarding.providers.get_chat_models", lambda provider: [])

    with pytest.raises(
        ModelResolutionError,
        match="configure an explicit model or retry when catalog discovery is available",
    ):
        resolve_catalog_model("provider")


def test_spec_harness_derivation() -> None:
    """Harness derives from ``config["harness"]`` then ``executor.type``.

    Mirrors the runner's ``_resolve_harness_config`` rule — a drift here
    would route a worker's provider resolution to the wrong harness.
    """
    assert spec_harness(_worker_spec("codex-native")) == "codex-native"
    assert (
        spec_harness(AgentSpec(spec_version=1, executor=ExecutorSpec(type="claude_sdk")))
        == "claude_sdk"
    )
    assert spec_harness(SimpleNamespace()) is None


def test_openai_compatible_listing_mints_bearer_via_auth_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A family with only ``auth_command`` mints its bearer via the shell.

    Dynamic-credential providers carry no static key at all — the
    enumerator must run the command and send its stdout as the bearer,
    or every such deployment silently degrades to ``none``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(
        monkeypatch,
        tmp_path,
        "providers:\n"
        "  mygw:\n"
        "    kind: gateway\n"
        "    default: true\n"
        "    openai:\n"
        "      base_url: https://gw.example.com/v1\n"
        "      auth_command: printf tok-from-cmd\n"
        "      wire_api: chat\n",
    )
    seen_auth: list[str | None] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Capture the Authorization header and serve one model."""
        seen_auth.append(request.headers.get("authorization"))
        return httpx.Response(
            200, json={"data": [{"id": "qwen/qwen3.7-plus", "context_length": 131072}]}
        )

    # pi has no family filter, so the qwen id must survive into the row.
    listing = list_models_for_worker(
        _worker_spec("pi"), "pi", transport=httpx.MockTransport(_handler)
    )

    # The minted token (the command's stdout, stripped) is the bearer.
    assert seen_auth == ["Bearer tok-from-cmd"]
    assert listing.source == "openai-compatible"
    assert [m.id for m in listing.models] == ["qwen/qwen3.7-plus"]


def test_keychain_credential_ref_degrades_not_crashes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``keychain:`` secret ref (deferred, unsupported) degrades cleanly.

    ``resolve_secret`` raises for keychain refs; the enumerator must
    surface that as a non-crashing degraded row whose note explains the
    credential problem instead of propagating the exception to the tool.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir.
    """
    _isolate_config(
        monkeypatch,
        tmp_path,
        "providers:\n"
        "  locked:\n"
        "    kind: gateway\n"
        "    default: true\n"
        "    openai:\n"
        "      base_url: https://gw.example.com/v1\n"
        "      api_key_ref: keychain:my-secret\n"
        "      wire_api: chat\n",
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        """Fail the test if any network call happens without a credential."""
        raise AssertionError("no HTTP call should be made when the secret is unresolvable")

    listing = list_models_for_worker(
        _worker_spec("codex-native"), "codex-native", transport=httpx.MockTransport(_handler)
    )

    assert listing.source == "none"
    assert not listing.models
    assert listing.note, "degraded keychain row must carry an explanatory note"
