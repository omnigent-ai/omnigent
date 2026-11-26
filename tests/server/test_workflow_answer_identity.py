"""How the server labels who sent a workflow answer, from facts it verified itself."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.server.session_journal import workflow_answer_identity

_COOKIE = "ap_session"
_BROWSER = "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15"


class _CookieAuth:
    """Accounts-mode auth: the session cookie or a bearer token names the user."""

    session_cookie_name = _COOKIE

    def get_user_id(self, request: Request) -> str | None:
        if request.cookies.get(_COOKIE) or request.headers.get("Authorization"):
            return "alice@example.com"
        return None


def _request(headers: dict[str, str]) -> Request:
    raw = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw})


@pytest.mark.parametrize(
    ("headers", "source", "authenticated_by"),
    [
        ({"Cookie": f"{_COOKIE}=jwt", "User-Agent": _BROWSER}, "web_form", "session_cookie"),
        (
            {"Cookie": f"{_COOKIE}=jwt", "User-Agent": _BROWSER, "X-Omnigent-Client": "desktop"},
            "web_form",
            "session_cookie",
        ),
        # Claiming to be the web app does not make a bearer token a person in the form.
        (
            {"Authorization": "Bearer jwt", "X-Omnigent-Client": "web", "User-Agent": _BROWSER},
            "api",
            "bearer",
        ),
        # A signed-in cookie from a script is still not the app's form.
        ({"Cookie": f"{_COOKIE}=jwt", "User-Agent": "curl/8.7.1"}, "api", "session_cookie"),
        # Anything the session's runner relays is the harness, whatever else it carries.
        (
            {
                "Cookie": f"{_COOKIE}=jwt",
                "User-Agent": _BROWSER,
                ORCHESTRATION_SOURCE_HEADER: "s1",
            },
            "harness",
            "session_cookie",
        ),
        ({"Authorization": "Bearer jwt", RUNNER_TUNNEL_TOKEN_HEADER: "t"}, "harness", "bearer"),
        ({"User-Agent": _BROWSER}, "api", "none"),
    ],
    ids=[
        "web_cookie",
        "desktop_cookie",
        "bearer_claiming_web",
        "cookie_from_script",
        "runner_relay",
        "runner_token",
        "anonymous",
    ],
)
def test_only_a_signed_in_app_session_counts_as_a_person_in_the_form(
    headers: dict[str, str], source: str, authenticated_by: str
) -> None:
    identity = workflow_answer_identity(_request(headers), _CookieAuth())  # type: ignore[arg-type]
    assert (identity["source"], identity["authenticated_by"]) == (source, authenticated_by)
    expected_user = None if authenticated_by == "none" else "alice@example.com"
    assert identity["answered_by"] == expected_user


def test_single_user_mode_names_nobody() -> None:
    identity = workflow_answer_identity(_request({"User-Agent": _BROWSER}), None)
    assert identity == {"source": "api", "authenticated_by": "none", "answered_by": None}
