"""Python session-list checks follow the actual request's query data flow."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from dev.lint._session_list_visibility_python import scan


@pytest.mark.parametrize(
    "source",
    [
        "client.sessions.list()",
        "await client.sessions.list(limit=20)",
        "client.sessions.list(**options)",
        "client.sessions.list(visibility=None)",
        'client.sessions.list(visibility="all" if flag else "")',
        "client.sessions.list(visibility=scope if flag else None)",
        'client.get("/v1/sessions")',
        'client.get("/sessions", params={"limit": 20})',
        'requests.request("GET", "https://example.org/v1/sessions")',
        'client.request(method="GET", url="/v1/sessions")',
        '_host_http_json(method="GET", path="/v1/sessions")',
        '_get(base, "/v1/sessions?limit=20", token)',
        '_get_json(f"{server}/v1/sessions?limit=20")',
        'urllib.request.urlopen(urllib.request.Request("https://example.org/v1/sessions"))',
        'client.get(base_url + "/v1/sessions")',
        'client.get("/v1/sessions", params=imported_builder())',
        'client.get("/v1/sessions?visibility=all", params={"limit": 20})',
        'client.get("/v1/sessions?visibility=all", params={})',
        'client.get("/v1/sessions?visibility=")',
        """
        unrelated = {"visibility": "all"}
        params = {"limit": 20}
        client.get("/v1/sessions", params=params)
        """,
        """
        params = {}
        client.get("/v1/sessions", params=params)
        params["visibility"] = "all"
        """,
        """
        params = {}
        if visibility:
            params["visibility"] = visibility
        client.get("/v1/sessions", params=params)
        """,
        """
        params = {"visibility": "all"}
        params = {"limit": 20}
        client.get("/v1/sessions", params=params)
        """,
        """
        params = {"visibility": "all"}
        del params["visibility"]
        client.get("/v1/sessions", params=params)
        """,
        """
        params = {"visibility": "all"}
        params.pop("visibility")
        client.get("/v1/sessions", params=params)
        """,
        """
        params = {"visibility": "all"}
        if flag:
            params["visibility"] = None
        client.get("/v1/sessions", params=params)
        """,
        """
        visibility = "all" if flag else None
        if visibility is not None:
            params = {"visibility": visibility}
        else:
            params = {}
        client.get("/v1/sessions", params=params)
        """,
        """
        def params():
            if optional:
                return {"visibility": "all"}
            return {"limit": 20}
        client.get("/v1/sessions", params=params())
        """,
    ],
)
def test_rejects_missing_or_unresolved_visibility(source: str) -> None:
    hits = scan(textwrap.dedent(source))
    assert hits
    assert all(line > 0 and "visibility" in message for line, message in hits)


@pytest.mark.parametrize(
    "source",
    [
        'client.sessions.list(visibility="mine")',
        "client.sessions.list(visibility=scope)",
        'client.sessions.list(**{"visibility": "all"})',
        'client.get("/v1/sessions", params={"visibility": "all"})',
        'client.get(f"{self._base}/v1/sessions", params={"visibility": visibility})',
        'client.get("/sessions?visibility=shared&limit=20")',
        'client.get(base_url + "/v1/sessions?visibility=all")',
        '_get(base, "/v1/sessions?visibility=all", token)',
        '_get_json(f"{server}/v1/sessions?visibility=all")',
        'client.get("/v1/sessions?visibility=all", params=None)',
        'client.post("/v1/sessions", json={})',
        'client.request("POST", "/v1/sessions", json={})',
        'urllib.request.Request("https://example.org/v1/sessions", data=body)',
        '_host_http_json(method="POST", path="/v1/sessions")',
        'client.get("/v1/sessions/conv_abc")',
        'client.get("/v1/sessions/projects")',
        'client.get(f"/v1/sessions/{session_id}")',
        'body.get("sessions")',
        '"client.sessions.list()"',
        '# client.get("/v1/sessions")',
        """
        @router.get("/sessions")
        def list_sessions():
            return []
        """,
        """
        params = {"visibility": "all"}
        if after:
            params["after"] = after
        client.get("/v1/sessions", params=params)
        """,
        """
        params = {"limit": 20}
        params["visibility"] = "mine"
        client.get("/v1/sessions", params=params)
        """,
        """
        params = {"limit": 20}
        params.update(visibility="all")
        client.get("/v1/sessions", params=params)
        """,
        """
        params = {"limit": 20}
        alias = params
        alias.update({"visibility": "all"})
        client.get("/v1/sessions", params=params)
        """,
        """
        params = {"limit": 20}
        params |= {"visibility": "all"}
        client.get("/v1/sessions", params=params)
        """,
        """
        params = dict(limit=20)
        client.get("/v1/sessions", params=dict(params, visibility="all"))
        """,
        """
        params = {"limit": 20}
        if optional:
            params["visibility"] = "mine"
        else:
            params["visibility"] = "all"
        client.get("/v1/sessions", params=params)
        """,
        """
        def params(after):
            result = {"visibility": "all"}
            if after:
                result["after"] = after
            return result
        _host_http_json(method="GET", path="/v1/sessions", params=params(after))
        """,
        """
        def _server_get(url, **kwargs):
            return client.get(url, **kwargs)
        _server_get("/v1/sessions", params={"visibility": "all"})
        """,
        """
        DEFAULT_QUERY = {"visibility": "all"}
        def params():
            return DEFAULT_QUERY.copy()
        client.get("/v1/sessions", params=params())
        """,
    ],
)
def test_accepts_explicit_queries_and_unrelated_calls(source: str) -> None:
    assert scan(textwrap.dedent(source)) == []


def test_visibility_in_other_function_does_not_hide_violation() -> None:
    source = """
    def list_mine():
        return client.get("/v1/sessions", params={"visibility": "mine"})

    def list_implicit():
        return client.get("/v1/sessions")
    """
    assert [line for line, _ in scan(textwrap.dedent(source))] == [6]


def test_specializes_generic_helper_for_sessions_path() -> None:
    source = """
    def fetch_page(path):
        params = {"limit": 20}
        if path == "/v1/sessions":
            params["visibility"] = "all"
        return client.get(path, params=params)

    fetch_page("/v1/agents")
    fetch_page("/v1/sessions")
    """
    assert scan(textwrap.dedent(source)) == []
    without_visibility = source.replace(
        '        if path == "/v1/sessions":\n            params["visibility"] = "all"\n', ""
    )
    assert scan(textwrap.dedent(without_visibility))


@pytest.mark.parametrize(
    ("relative", "assignment"),
    [
        (
            "sdks/python-client/omnigent_client/_sessions.py",
            '            "visibility": visibility,\n',
        ),
        ("omnigent/cli.py", '        "visibility": "all",\n'),
        (
            "omnigent/runner/tool_dispatch.py",
            '        if path == "/v1/sessions":\n            params["visibility"] = "all"\n',
        ),
    ],
)
def test_current_request_builders_and_removing_visibility(relative: str, assignment: str) -> None:
    """Check real indirect callers, then prove removing their query field is rejected."""
    root = Path(__file__).resolve().parents[3]
    source = (root / relative).read_text()
    assert assignment in source
    assert scan(source) == []
    assert scan(source.replace(assignment, "", 1))


def test_invalid_python_is_left_to_syntax_checks() -> None:
    assert scan("async def unfinished(") == []
