"""Shared test helpers for the native-harness policy hooks.

Used by both ``tests/test_claude_native_hook.py`` and
``tests/test_codex_native_hook.py`` so the fail-closed failure-mode stub
lives in one place and can't drift if new modes are added.
"""

from __future__ import annotations

import httpx


def make_failing_client(mode: str) -> type:
    """
    Build an ``httpx.Client`` stub that fails the policy POST a given way.

    :param mode: One of ``"connect_error"`` (POST raises), ``"non_2xx"``
        (503 → ``raise_for_status``), ``"empty_body"`` (200, no content),
        or ``"malformed_json"`` (200, non-JSON body).
    :returns: A class usable as a drop-in for :class:`httpx.Client`.
    """

    class _FailingHttpxClient:
        def __init__(self, *, headers: dict[str, str], timeout: object) -> None:
            del headers, timeout

        def __enter__(self) -> _FailingHttpxClient:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
            del json
            req = httpx.Request("POST", url)
            if mode == "connect_error":
                raise httpx.ConnectError("AP unreachable", request=req)
            if mode == "non_2xx":
                return httpx.Response(503, text="upstream down", request=req)
            if mode == "empty_body":
                return httpx.Response(200, content=b"", request=req)
            return httpx.Response(200, text="not json at all", request=req)

    return _FailingHttpxClient
