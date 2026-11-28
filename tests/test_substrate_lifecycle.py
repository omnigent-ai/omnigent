"""Unit checks for the deployed lifecycle script's failure cleanup."""

import sys

import httpx
import pytest

from tests.e2e.integrations.deploy.substrate.e2e_lifecycle import cleanup


@pytest.mark.parametrize("original_failure", [False, True])
def test_cleanup_attempts_both_deletes_and_preserves_failure(original_failure):
    requests = []
    deletion_error = httpx.ConnectError("session deletion failed")

    def delete(request):
        requests.append(request.url.path)
        if request.url.path == "/v1/sessions/session":
            raise deletion_error
        return httpx.Response(500)

    original_error = AssertionError("lifecycle failed")
    expected = AssertionError if original_failure else ExceptionGroup
    with (
        httpx.Client(
            base_url="https://server.example.com", transport=httpx.MockTransport(delete)
        ) as client,
        pytest.raises(expected) as caught,
    ):
        try:
            if original_failure:
                raise original_error
        finally:
            cleanup(client, "session", "host", sys.exception())

    assert requests == ["/v1/sessions/session", "/v1/hosts/host"]
    if original_failure:
        assert caught.value is original_error
        assert len(original_error.__notes__) == 2
        assert "session deletion failed" in original_error.__notes__[0]
        assert "500" in original_error.__notes__[1]
    else:
        assert caught.value.exceptions[0] is deletion_error
        assert isinstance(caught.value.exceptions[1], httpx.HTTPStatusError)


def test_cleanup_accepts_deleted_host_and_skips_uncreated_session():
    requests = []

    def delete(request):
        requests.append(request.url.path)
        return httpx.Response(404)

    with httpx.Client(
        base_url="https://server.example.com", transport=httpx.MockTransport(delete)
    ) as client:
        cleanup(client, None, "host", None)
    assert requests == ["/v1/hosts/host"]
