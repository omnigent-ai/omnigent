"""Tests for the :class:`omnigent.server_url.ServerUrl` value type.

The one representation of an Omnigent server URL: requests target
``api_base``, user-facing messages show ``display``. These tests pin the
mapping between the two and the ``?o=`` (SPOG workspace selector)
threading, so a regression can't leak the internal API mount back into
user-facing text or drop the selector from copy-pasteable URLs.
"""

from __future__ import annotations

import pytest

from omnigent.server_url import (
    ServerUrl,
    display_server_url,
    is_workspace_hosted_url,
    org_id_from_url,
)

_WORKSPACE_API = "https://ws.databricks.com/api/2.0/omnigent"


def test_api_base_is_normalized() -> None:
    """A trailing slash is trimmed so equality and store keys are stable."""
    assert ServerUrl(f"{_WORKSPACE_API}/").api_base == _WORKSPACE_API


def test_direct_construction_strips_query_into_org_id() -> None:
    """``api_base`` never carries a query — callers append request paths to it.

    A ``?o=`` slipped into a direct construction becomes the org id instead of
    corrupting later ``f"{api_base}/v1/…"`` URLs; an explicitly passed org id
    wins over the URL's own selector.
    """
    absorbed = ServerUrl(f"{_WORKSPACE_API}?o=123")
    assert absorbed.api_base == _WORKSPACE_API
    assert absorbed.org_id == "123"

    explicit = ServerUrl(f"{_WORKSPACE_API}?o=123", org_id="456")
    assert explicit.api_base == _WORKSPACE_API
    assert explicit.org_id == "456"


@pytest.mark.parametrize(
    "api_base,org_id,expected",
    [
        # Workspace-hosted: the UI mount the user recognizes, with the
        # selector when known so a copy-paste routes to the right workspace.
        (_WORKSPACE_API, "123", "https://ws.databricks.com/omnigent?o=123"),
        (_WORKSPACE_API, None, "https://ws.databricks.com/omnigent"),
        # Everything else is shown as-is — including Databricks Apps hosts,
        # whose URL carries no internal path to hide.
        ("http://127.0.0.1:6767", None, "http://127.0.0.1:6767"),
        (
            "https://myapp-123.aws.databricksapps.com",
            "123",
            "https://myapp-123.aws.databricksapps.com",
        ),
    ],
)
def test_display(api_base: str, org_id: str | None, expected: str) -> None:
    """``display`` hides the API mount and carries ``?o=`` when known."""
    assert ServerUrl(api_base, org_id=org_id).display == expected


def test_workspace_host() -> None:
    """The fronting workspace origin resolves only for workspace mounts."""
    assert ServerUrl(_WORKSPACE_API).workspace_host == "https://ws.databricks.com"
    assert ServerUrl("http://127.0.0.1:6767").workspace_host is None


def test_from_api_base_prefers_url_selector_over_store(tmp_path, monkeypatch) -> None:
    """A ``?o=`` on the URL itself wins; the query is stripped off api_base.

    The explicit selector is the user's freshest intent — a stale stored
    record must not override it, and the wire base must stay query-free
    (callers append paths like ``/v1/me``).
    """
    from omnigent.cli_auth import store_databricks_auth

    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path",
        lambda: tmp_path / "auth_tokens.json",
    )
    store_databricks_auth(_WORKSPACE_API, "https://ws.databricks.com", org_id="999")

    resolved = ServerUrl.from_api_base(f"{_WORKSPACE_API}?o=123")

    assert resolved.api_base == _WORKSPACE_API
    assert resolved.org_id == "123"


def test_from_api_base_falls_back_to_login_record(tmp_path, monkeypatch) -> None:
    """Without a URL selector, the ``omnigent login`` record supplies it."""
    from omnigent.cli_auth import store_databricks_auth

    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path",
        lambda: tmp_path / "auth_tokens.json",
    )
    store_databricks_auth(_WORKSPACE_API, "https://ws.databricks.com", org_id="999")

    resolved = ServerUrl.from_api_base(_WORKSPACE_API)

    assert resolved.org_id == "999"
    assert resolved.display == "https://ws.databricks.com/omnigent?o=999"


def test_display_round_trips_through_login_resolution(monkeypatch) -> None:
    """The display URL, pasted into ``omnigent login``, resolves back.

    The whole point of showing ``https://<ws>/omnigent?o=<org>`` in
    messages and login hints is that copy-pasting it reaches the same
    server: the resolver expands the UI mount back to the API base and
    recaptures the selector.
    """
    import omnigent.cli as cli_mod

    def fake_get(url: str, **kwargs: object):
        import httpx

        request = httpx.Request("GET", url)
        if url == "https://ws.databricks.com/v1/me":
            return httpx.Response(404, headers={"server": "databricks"}, request=request)
        if url == f"{_WORKSPACE_API}/v1/me":
            return httpx.Response(
                401,
                headers={"www-authenticate": 'Bearer realm="DatabricksRealm"'},
                request=request,
            )
        raise AssertionError(f"unexpected probe {url}")

    monkeypatch.setattr("httpx.get", fake_get)
    shown = ServerUrl(_WORKSPACE_API, org_id="123").display

    resolved = cli_mod._resolve_server_url(shown)

    assert resolved.api_base == _WORKSPACE_API
    assert resolved.org_id == "123"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://acme.databricks.com/?o=123", "123"),
        ("https://acme.databricks.com", None),
        ("https://acme.databricks.com/?o=", None),
    ],
)
def test_org_id_from_url(url: str, expected: str | None) -> None:
    """The ``?o=`` selector parses out of a raw URL, absent → ``None``."""
    assert org_id_from_url(url) == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        # Databricks workspace API mount → the recognizable /omnigent SPA URL.
        (
            "https://e2-dogfood.staging.cloud.databricks.com/api/2.0/omnigent",
            "https://e2-dogfood.staging.cloud.databricks.com/omnigent",
        ),
        # A trailing ``?o=<org>`` selector on the API base is kept on the
        # display URL so a copy-paste still routes to the right workspace.
        (
            "https://ws.databricks.com/api/2.0/omnigent?o=123",
            "https://ws.databricks.com/omnigent?o=123",
        ),
        # Trailing slash on the API mount still maps cleanly.
        (
            "https://ws.databricks.com/api/2.0/omnigent/",
            "https://ws.databricks.com/omnigent",
        ),
        # Non-Databricks URLs pass through unchanged (sans trailing slash).
        ("http://127.0.0.1:6767", "http://127.0.0.1:6767"),
        ("https://omnigent-02m5.onrender.com/", "https://omnigent-02m5.onrender.com"),
    ],
)
def test_display_server_url_maps_databricks_api_mount(url: str, expected: str) -> None:
    """The plain-string wrapper rewrites the Databricks API mount to the SPA URL.

    What this proves: the startup banner shows the workspace ``/omnigent``
    URL a user recognizes instead of the internal ``/api/2.0/omnigent``
    proxy path, while every other target is shown verbatim. A regression
    that stopped mapping would leak the API path back into the banner.
    """
    assert display_server_url(url) == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://ws.databricks.com/api/2.0/omnigent", True),
        ("https://ws.databricks.com/api/2.0/omnigent/", True),
        ("https://ws.databricks.com/omnigent", False),  # the SPA URL, not the API mount
        ("http://127.0.0.1:6767", False),
        ("https://omnigent-02m5.onrender.com", False),
    ],
)
def test_is_workspace_hosted_url(url: str, expected: bool) -> None:
    """``is_workspace_hosted_url`` is true only for the workspace API mount.

    What this proves: the predicate the REPL banner uses to suppress the
    server-version row fires for ``/api/2.0/omnigent`` and nothing else, so
    non-Databricks targets keep showing their version.
    """
    assert is_workspace_hosted_url(url) is expected
