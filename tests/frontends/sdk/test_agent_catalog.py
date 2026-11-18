"""Catalog policy, independent of the CLI and SDK HTTP transports."""

from __future__ import annotations

import pytest
from omnigent_client import OmnigentError, RegisteredAgentCatalog


@pytest.mark.parametrize(
    "row",
    [
        None,
        "bad",
        {},
        {"id": "ag_x"},
        {"name": "other"},
        {"id": "ag_x", "name": 7},
        {"id": 7, "name": "other"},
        {"id": "", "name": "other"},
        {"id": "ag_x", "name": ""},
        {"id": " ", "name": "other"},
        {"id": "ag_x", "name": " "},
    ],
)
@pytest.mark.parametrize("scope", ["server", "user"])
def test_catalog_rejects_malformed_rows_before_selecting(row: object, scope: str) -> None:
    catalog = RegisteredAgentCatalog(stop_at="good")
    if scope == "user":
        catalog.add_page({"data": []})
    with pytest.raises(OmnigentError, match="malformed agent row"):
        catalog.add_page({"data": [{"id": "ag_good", "name": "good"}, row]})
    assert not catalog.done


@pytest.mark.parametrize("page", [{}, {"data": None}, {"data": {}}])
def test_catalog_requires_a_data_list(page: dict[str, object]) -> None:
    with pytest.raises(OmnigentError, match="unexpected body shape"):
        RegisteredAgentCatalog().add_page(page)


@pytest.mark.parametrize("scope", ["server", "user"])
@pytest.mark.parametrize("cursor", [None, "", " ", 7, "first", "second"])
def test_catalog_rejects_missing_or_repeated_cursors(scope: str, cursor: object) -> None:
    catalog = RegisteredAgentCatalog()
    if scope == "user":
        catalog.add_page({"data": []})
    for previous in ("first", "second"):
        catalog.add_page({"data": [], "has_more": True, "last_id": previous})
        assert catalog.params["after"] == previous
    with pytest.raises(OmnigentError, match="no usable cursor"):
        catalog.add_page({"data": [], "has_more": True, "last_id": cursor})


@pytest.mark.parametrize("same_registration", [False, True])
def test_catalog_preserves_server_identity_and_resets_cursor(same_registration: bool) -> None:
    catalog = RegisteredAgentCatalog()
    server = {"id": "ag_server", "name": "scribe", "harness": "claude-sdk"}
    user = server if same_registration else {**server, "id": "ag_user", "harness": "codex"}
    catalog.add_page({"data": [server], "has_more": True, "last_id": "next"})
    catalog.add_page({"data": []})
    assert catalog.params == {"limit": 1000, "scope": "user"}
    catalog.add_page({"data": [user], "has_more": True, "last_id": "next"})
    catalog.add_page({"data": [{"id": "ag_mine", "name": "mine"}]})
    assert catalog.done
    assert [(a.id, a.harness, a.source) for a in catalog.entries.values()] == [
        ("ag_server", "claude-sdk", "server"),
        ("ag_mine", None, "user"),
    ]


@pytest.mark.parametrize("status", [401, 403, 404, 422, 500, None])
@pytest.mark.parametrize("scope", ["server", "user"])
def test_only_optional_scope_compatibility_errors_are_ignored(
    status: int | None, scope: str
) -> None:
    catalog = RegisteredAgentCatalog()
    catalog.add_page(
        {"data": [{"id": "ag_s", "name": "server"}], "has_more": True, "last_id": "s"}
    )
    if scope == "user":
        catalog.add_page({"data": []})
        catalog.add_page(
            {"data": [{"id": "ag_u", "name": "user"}], "has_more": True, "last_id": "u"}
        )
    ignored = scope == "user" and status in (401, 403, 404, 422)
    assert catalog.skip_unavailable_scope(status) is ignored
    assert catalog.done is ignored
    if ignored:
        assert list(catalog.entries) == ["server"]
