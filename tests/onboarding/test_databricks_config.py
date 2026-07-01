"""Unit tests for omnigent.onboarding.databricks_config."""

from __future__ import annotations

import configparser
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.onboarding.databricks_config import (
    databricks_sdk_installed,
    get_workspace_url_for_profile,
    list_claude_model_service_fqns,
    list_claude_serving_endpoint_names,
    normalize_workspace_url,
)

_WORKSPACE_URL = "https://example.databricks.com"


def test_get_workspace_url_for_profile_reads_databrickscfg(tmp_path: Path) -> None:
    """Resolves a profile name to its host from ~/.databrickscfg."""
    cfg = configparser.ConfigParser()
    cfg["test-profile"] = {"host": _WORKSPACE_URL, "token": "tok"}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        url = get_workspace_url_for_profile("test-profile")

    assert url == _WORKSPACE_URL


def test_get_workspace_url_for_profile_strips_trailing_slash(tmp_path: Path) -> None:
    """Host values with a trailing slash are normalized."""
    cfg = configparser.ConfigParser()
    cfg["test-profile"] = {"host": _WORKSPACE_URL + "/", "token": "tok"}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        url = get_workspace_url_for_profile("test-profile")

    assert url == _WORKSPACE_URL


def test_get_workspace_url_for_profile_returns_none_when_file_absent(
    tmp_path: Path,
) -> None:
    """Returns None when ~/.databrickscfg does not exist."""
    with patch(
        "omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH",
        tmp_path / "nonexistent",
    ):
        assert get_workspace_url_for_profile("test-profile") is None


def test_get_workspace_url_for_profile_returns_none_for_missing_profile(
    tmp_path: Path,
) -> None:
    """Returns None when the named profile is not in ~/.databrickscfg."""
    cfg = configparser.ConfigParser()
    cfg["other"] = {"host": "https://example-other.cloud.databricks.com"}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        assert get_workspace_url_for_profile("test-profile") is None


def test_get_workspace_url_for_profile_does_not_use_default_for_missing_profile(
    tmp_path: Path,
) -> None:
    """A typo'd profile must not silently resolve to the DEFAULT workspace."""
    cfg = configparser.ConfigParser()
    cfg["DEFAULT"] = {"host": _WORKSPACE_URL}
    cfg["other"] = {"host": "https://example-other.cloud.databricks.com"}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        assert get_workspace_url_for_profile("test-profile") is None


def test_get_workspace_url_for_profile_reads_explicit_default_profile(
    tmp_path: Path,
) -> None:
    """The DEFAULT section is only used when the caller asks for DEFAULT."""
    cfg = configparser.ConfigParser()
    cfg["DEFAULT"] = {"host": _WORKSPACE_URL}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        url = get_workspace_url_for_profile("DEFAULT")

    assert url == _WORKSPACE_URL


def test_get_workspace_url_for_profile_reads_lowercase_default_profile(
    tmp_path: Path,
) -> None:
    """The Databricks SDK treats ``default`` as the DEFAULT profile name."""
    cfg = configparser.ConfigParser()
    cfg["DEFAULT"] = {"host": _WORKSPACE_URL}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        url = get_workspace_url_for_profile("default")

    assert url == _WORKSPACE_URL


def test_databricks_sdk_installed_true_in_dev_env() -> None:
    """``databricks_sdk_installed`` finds the SDK in the dev environment.

    The dev/CI install carries ``databricks-sdk`` (via the ``all`` extra),
    so the helper must report it present. A failure means the helper probes
    the wrong module path (e.g. a typo'd ``find_spec`` target), which would
    make the add-provider menu and ``setup --internal-beta`` claim the
    Databricks extra is missing even on installs that have it.
    """
    assert databricks_sdk_installed() is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The reported foot-gun: the URL copied from a browser address bar.
        (
            "https://my-ws.cloud.databricks.com/browse?o=1234567890",
            "https://my-ws.cloud.databricks.com",
        ),
        # Path with no query.
        (
            "https://my-ws.cloud.databricks.com/explore/data",
            "https://my-ws.cloud.databricks.com",
        ),
        # Fragment is dropped too.
        (
            "https://my-ws.cloud.databricks.com/#/setting/account",
            "https://my-ws.cloud.databricks.com",
        ),
        # Surrounding whitespace is trimmed before parsing.
        (
            "  https://my-ws.cloud.databricks.com/browse  ",
            "https://my-ws.cloud.databricks.com",
        ),
        # Pre-existing trailing-slash case still collapses.
        ("https://my-ws.cloud.databricks.com/", "https://my-ws.cloud.databricks.com"),
        # Already an origin — returned unchanged.
        ("https://my-ws.cloud.databricks.com", "https://my-ws.cloud.databricks.com"),
    ],
)
def test_normalize_workspace_url_reduces_to_origin(raw: str, expected: str) -> None:
    """A pasted workspace URL is reduced to its bare ``scheme://host`` origin."""
    assert normalize_workspace_url(raw) == expected


def test_normalize_workspace_url_scheme_less_input_only_strips_trailing_slash() -> None:
    """Without a scheme there is no netloc to isolate, so the result matches the
    prior ``rstrip("/")`` behavior — the wizard pre-adds ``https://`` before
    calling, so a scheme is present in practice."""
    assert normalize_workspace_url("my-ws.cloud.databricks.com/") == "my-ws.cloud.databricks.com"


def _fake_workspace_client(endpoints: list[dict], permission_levels: dict[str, str]):
    """Build a WorkspaceClient stand-in for the endpoint-listing tests.

    :param endpoints: ``as_dict()``-shaped list items (``name`` / ``task`` /
        ``config.served_entities``).
    :param permission_levels: Caller's permission per endpoint name, e.g.
        ``{"databricks-claude-opus-4-8": "CAN_QUERY"}``; a missing name makes
        the detail ``get()`` raise (endpoint gone mid-sweep).
    """

    class _Level:
        def __init__(self, value: str) -> None:
            self.value = value

    class _ListItem:
        def __init__(self, raw: dict) -> None:
            self.name = raw.get("name")
            self.task = raw.get("task")
            self._raw = raw

        def as_dict(self) -> dict:
            return self._raw

    class _Detail:
        def __init__(self, name: str) -> None:
            self.permission_level = _Level(permission_levels[name])

    class _ServingEndpoints:
        def list(self) -> list[_ListItem]:
            return [_ListItem(raw) for raw in endpoints]

        def get(self, name: str) -> _Detail:
            return _Detail(name)

    class _Client:
        def __init__(self, *, profile: str) -> None:
            assert profile == "my-ws"
            self.serving_endpoints = _ServingEndpoints()

    return _Client


def _chat_endpoint(name: str, *, model_name: str) -> dict:
    """One ``llm/v1/chat`` list item serving a single foundation model."""
    return {
        "name": name,
        "task": "llm/v1/chat",
        "config": {"served_entities": [{"foundation_model": {"name": model_name}}]},
    }


def test_list_claude_serving_endpoint_names_filters_to_queryable_claude_chat() -> None:
    """Only Claude-serving chat endpoints with query access survive, sorted.

    The picker's picks feed the gateway's Anthropic surface, which rejects
    non-Claude endpoints ("API type 'anthropic/v1/messages' is not
    supported") — so a GPT chat endpoint, an embeddings endpoint (even a
    hypothetical Claude-named one), and a CAN_VIEW-only Claude endpoint
    must all be dropped. Offering any of them would hand the user a pick
    that fails at request time.
    """
    endpoints = [
        _chat_endpoint("zeta-claude", model_name="system.ai.databricks-claude-opus-4-8"),
        _chat_endpoint("alpha-claude", model_name="system.ai.databricks-claude-sonnet-5"),
        _chat_endpoint("view-only-claude", model_name="system.ai.databricks-claude-haiku-4-5"),
        _chat_endpoint("gpt-chat", model_name="system.ai.databricks-gpt-5-5"),
        {
            "name": "claude-embeddings",
            "task": "llm/v1/embeddings",
            "config": {"served_entities": [{"foundation_model": {"name": "claude-embedder"}}]},
        },
        {"name": None, "task": "llm/v1/chat", "config": {}},
    ]
    levels = {
        "zeta-claude": "CAN_QUERY",
        "alpha-claude": "CAN_MANAGE",
        "view-only-claude": "CAN_VIEW",
        "gpt-chat": "CAN_MANAGE",
        "claude-embeddings": "CAN_MANAGE",
    }
    with patch("databricks.sdk.WorkspaceClient", _fake_workspace_client(endpoints, levels)):
        assert list_claude_serving_endpoint_names("my-ws") == ["alpha-claude", "zeta-claude"]


def test_list_claude_serving_endpoint_names_accepts_anthropic_external_model() -> None:
    """An external-model endpoint with the ``anthropic`` provider counts as Claude.

    Workspaces routing Claude through an external-model endpoint (rather
    than a Databricks-hosted foundation model) must still be offered.
    """
    endpoints = [
        {
            "name": "my-external-anthropic",
            "task": "llm/v1/chat",
            "config": {
                "served_entities": [
                    {"external_model": {"provider": "anthropic", "name": "claude-opus-4-8"}}
                ]
            },
        },
    ]
    levels = {"my-external-anthropic": "CAN_QUERY"}
    with patch("databricks.sdk.WorkspaceClient", _fake_workspace_client(endpoints, levels)):
        assert list_claude_serving_endpoint_names("my-ws") == ["my-external-anthropic"]


def test_list_claude_serving_endpoint_names_drops_endpoint_when_detail_fails() -> None:
    """A failing per-endpoint permission read drops that endpoint, not the list.

    The detail sweep runs one ``get()`` per candidate; an endpoint deleted
    mid-sweep (or otherwise unreadable) must be skipped while the rest of
    the list survives.
    """
    endpoints = [
        _chat_endpoint("ok-claude", model_name="system.ai.databricks-claude-opus-4-8"),
        _chat_endpoint("gone-claude", model_name="system.ai.databricks-claude-sonnet-5"),
    ]
    levels = {"ok-claude": "CAN_QUERY"}  # "gone-claude" missing → get() raises KeyError
    with patch("databricks.sdk.WorkspaceClient", _fake_workspace_client(endpoints, levels)):
        assert list_claude_serving_endpoint_names("my-ws") == ["ok-claude"]


def test_list_claude_serving_endpoint_names_empty_on_failure() -> None:
    """Any wholesale listing failure degrades to an empty list, never an exception.

    The picker treats ``[]`` as "offer free-text entry only" — a raising
    helper would abort the whole add/edit flow on a network or auth
    hiccup.
    """

    class _RaisingClient:
        def __init__(self, *, profile: str) -> None:
            raise RuntimeError("no auth")

    with patch("databricks.sdk.WorkspaceClient", _RaisingClient):
        assert list_claude_serving_endpoint_names("my-ws") == []


def _fake_model_services_client(pages: list[dict], details: dict[str, dict] | None = None):
    """Build a WorkspaceClient stand-in whose raw API serves *pages* in order.

    :param pages: Raw ``unity-catalog/model-services`` list responses; each
        after the first is served when the prior page's ``next_page_token``
        is echoed back in the request path.
    :param details: Single-get responses keyed by FQN (for the detail sweep
        of services the list payload cannot decide); a missing FQN raises,
        like a service deleted mid-sweep.
    """

    class _ApiClient:
        def __init__(self) -> None:
            self.pages_served = 0

        def do(self, method: str, path: str) -> dict:
            assert method == "GET"
            tail = path.split("/model-services", 1)[1]
            if tail.startswith("/"):
                return (details or {})[tail[1:]]
            self.pages_served += 1
            return pages[self.pages_served - 1]

    class _Client:
        def __init__(self, *, profile: str) -> None:
            assert profile == "my-ws"
            self.api_client = _ApiClient()

    return _Client


def _model_service(fqn: str, *, api_types: list[str] | None = None, dest_model: str = "") -> dict:
    """One raw model-service entry (``api_types=None`` omits the field)."""
    entry: dict = {"name": f"model-services/{fqn}", "config": {}}
    if api_types is not None:
        entry["supported_api_types"] = api_types
    if dest_model:
        entry["config"] = {
            "destinations": [{"pay_per_token_config": {"model": f"models/{dest_model}"}}]
        }
    return entry


def test_list_claude_model_service_fqns_filters_and_strips() -> None:
    """Only Claude-capable, non-system custom services survive, FQN-sorted.

    ``supported_api_types`` is authoritative when populated, but live
    user-created services report it EMPTY while answering
    ``anthropic/v1/messages`` fine — so a Claude routing destination must
    also qualify. ``system.ai`` services mirror the hosted serving
    endpoints the picker already lists, so they are dropped, as are
    GPT-routing services (the Anthropic surface rejects them).
    """
    pages = [
        {
            "model_services": [
                # Authoritative api-types match.
                _model_service("main.agents.zeta", api_types=["anthropic/v1/messages"]),
                # Live shape: empty api types, Claude destination.
                _model_service(
                    "main.agents.alpha",
                    api_types=[],
                    dest_model="system.ai.databricks-claude-opus-4-8",
                ),
                # GPT destination — not Anthropic-surface capable.
                _model_service(
                    "main.agents.gpt", api_types=[], dest_model="system.ai.databricks-gpt-5-5"
                ),
                # Built-in mirror of the hosted endpoints — dropped.
                _model_service("system.ai.claude-opus-4-8", api_types=["anthropic/v1/messages"]),
                # Explicit non-Anthropic api types — dropped.
                _model_service("main.agents.embed", api_types=["mlflow/v1/embeddings"]),
            ]
        }
    ]
    with patch("databricks.sdk.WorkspaceClient", _fake_model_services_client(pages)):
        assert list_claude_model_service_fqns("my-ws") == [
            "main.agents.alpha",
            "main.agents.zeta",
        ]


def test_list_claude_model_service_fqns_follows_pagination() -> None:
    """A ``next_page_token`` is followed until the listing is exhausted."""
    pages = [
        {
            "model_services": [
                _model_service("main.agents.first", api_types=["anthropic/v1/messages"])
            ],
            "next_page_token": "tok1",
        },
        {
            "model_services": [
                _model_service("main.agents.second", api_types=["anthropic/v1/messages"])
            ]
        },
    ]
    with patch("databricks.sdk.WorkspaceClient", _fake_model_services_client(pages)):
        assert list_claude_model_service_fqns("my-ws") == [
            "main.agents.first",
            "main.agents.second",
        ]


def test_list_claude_model_service_fqns_detail_sweep_decides_bare_entries() -> None:
    """List entries with no api types and no destinations use their detail.

    The real list response omits routing destinations (observed live), so
    a bare entry is undecidable from the list alone — the helper must
    fetch its single-get detail: a Claude destination qualifies, a GPT one
    does not, and an unreadable detail drops just that service.
    """
    pages = [
        {
            "model_services": [
                _model_service("main.agents.opus"),
                _model_service("main.agents.gpt"),
                _model_service("main.agents.gone"),
            ]
        }
    ]
    details = {
        "main.agents.opus": _model_service(
            "main.agents.opus", api_types=[], dest_model="system.ai.databricks-claude-opus-4-8"
        ),
        "main.agents.gpt": _model_service(
            "main.agents.gpt", api_types=[], dest_model="system.ai.databricks-gpt-5-5"
        ),
        # "main.agents.gone" missing → detail get raises (deleted mid-sweep).
    }
    with patch("databricks.sdk.WorkspaceClient", _fake_model_services_client(pages, details)):
        assert list_claude_model_service_fqns("my-ws") == ["main.agents.opus"]


def test_list_claude_model_service_fqns_empty_on_failure() -> None:
    """A failing (e.g. pre-Beta workspace) model-services API degrades to ``[]``.

    The picker then still offers the hosted section and free-text entry —
    a raising helper would abort the whole add/edit flow.
    """

    class _RaisingClient:
        def __init__(self, *, profile: str) -> None:
            raise RuntimeError("ENDPOINT_NOT_FOUND")

    with patch("databricks.sdk.WorkspaceClient", _RaisingClient):
        assert list_claude_model_service_fqns("my-ws") == []


def test_list_claude_endpoints_shares_one_client_and_degrades_independently() -> None:
    """The combined listing serves both sections from one client.

    One client (one OAuth handshake) feeds both listings in parallel; a
    failure in one listing must not take the other down — here the
    serving-endpoints listing raises while the UC model services still
    return.
    """

    class _RaisingServingEndpoints:
        def list(self) -> list:
            raise RuntimeError("serving endpoints unavailable")

    class _ApiClient:
        def do(self, method: str, path: str) -> dict:
            return {
                "model_services": [
                    {
                        "name": "model-services/main.agents.opus",
                        "supported_api_types": ["anthropic/v1/messages"],
                        "config": {},
                    }
                ]
            }

    constructed = []

    class _Client:
        def __init__(self, *, profile: str) -> None:
            constructed.append(profile)
            self.serving_endpoints = _RaisingServingEndpoints()
            self.api_client = _ApiClient()

    from omnigent.onboarding.databricks_config import list_claude_endpoints

    with patch("databricks.sdk.WorkspaceClient", _Client):
        hosted, custom = list_claude_endpoints("my-ws")

    assert hosted == []
    assert custom == ["main.agents.opus"]
    # Exactly one client was built for both listings.
    assert constructed == ["my-ws"]


def test_list_claude_endpoints_empty_on_client_failure() -> None:
    """An unusable profile degrades both sections to empty lists."""

    class _RaisingClient:
        def __init__(self, *, profile: str) -> None:
            raise RuntimeError("no such profile")

    from omnigent.onboarding.databricks_config import list_claude_endpoints

    with patch("databricks.sdk.WorkspaceClient", _RaisingClient):
        assert list_claude_endpoints("my-ws") == ([], [])


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("databricks-claude-opus-4-8", "1M"),
        ("databricks-claude-opus-4-7", "1M"),
        ("databricks-claude-opus-4-6", "1M"),
        ("databricks-claude-sonnet-5", "1M"),
        ("databricks-claude-sonnet-4-6", "1M"),
        ("main.agents.opus-4-8", "1M"),  # UC FQNs usually carry the family too
        ("databricks-claude-opus-4-5", "200K"),
        ("databricks-claude-opus-4-1", "200K"),
        ("databricks-claude-sonnet-4-5", "200K"),
        ("databricks-claude-sonnet-4", "200K"),
        ("databricks-claude-haiku-4-5", "200K"),
        # No family token in the name — window unknowable, no label/pin.
        ("main.agents.my-opus-endpoint", None),
        ("my-custom-endpoint", None),
    ],
)
def test_claude_context_window_by_family(name: str, expected: str | None) -> None:
    """Window lookup matches model families as name substrings, most specific first.

    Drives the picker's context-window labels and the [1m] auto-pin: a
    wrong row here either hides the 1M window from a capable model or —
    worse — pins a 200K model at a window it doesn't have.
    """
    from omnigent.onboarding.databricks_config import claude_context_window

    assert claude_context_window(name) == expected
