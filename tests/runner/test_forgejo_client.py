"""Forgejo REST reads send each CLI token only to the authority of the login it came from."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from omnigent.git_providers import forgejo as descriptor
from omnigent.runner import forgejo_client
from omnigent.runner.forgejo_client import (
    ForgejoClient,
    ForgejoError,
    ForgejoLogin,
    ForgejoTimeoutError,
    logins,
)

FJ_TOKEN = "fj-secret-token"
TEA_TOKEN = "tea-secret-token"


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A home whose fj and tea logins name distinct authorities."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(descriptor.sys, "platform", "linux")
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_FORGEJO_HOSTS", "configured.test,fj.test")
    keys = home / ".local/share/forgejo-cli/keys.json"
    keys.parent.mkdir(parents=True)
    keys.write_text(
        json.dumps(
            {
                "hosts": {
                    "fj.test:3443": {"type": "Application", "token": FJ_TOKEN},
                    "both.test": {"type": "OAuth", "token": FJ_TOKEN, "refresh_token": "r"},
                }
            }
        ),
        encoding="utf-8",
    )
    tea = home / ".config/tea/config.yml"
    tea.parent.mkdir(parents=True)
    tea.write_text(
        "logins:\n"
        f"    - name: tea\n      url: https://tea.test\n      token: {TEA_TOKEN}\n"
        f"    - name: both\n      url: https://both.test\n      token: {TEA_TOKEN}\n"
        "    - name: oauth\n      url: https://oauth.test\n      token: stale\n"
        "      auth_method: oauth\n"
        "    - name: plain\n      url: http://plain.test\n      token: plain\n",
        encoding="utf-8",
    )
    descriptor._login_authorities.cache_clear()
    forgejo_client._cli_logins.cache_clear()
    return home


Handler = Callable[[httpx.Request], httpx.Response]


class Recorder(httpx.MockTransport):
    """Record every request that reaches the transport."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.requests: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request) if handler else httpx.Response(200, json={"ok": True})

        super().__init__(record)


def test_login_table_tracks_the_source_of_each_token() -> None:
    table = logins()
    assert {authority: (login.token, login.source) for authority, login in table.items()} == {
        "codeberg.org": (None, "configured"),
        "configured.test": (None, "configured"),
        "fj.test": (None, "configured"),
        "fj.test:3443": (FJ_TOKEN, "forgejo-cli"),
        "tea.test": (TEA_TOKEN, "tea"),
        "both.test": (FJ_TOKEN, "forgejo-cli"),
        "oauth.test": (None, "configured"),
    }
    assert FJ_TOKEN not in repr(table)


@pytest.mark.parametrize(
    "host,authorization",
    [
        ("fj.test:3443", f"token {FJ_TOKEN}"),
        ("tea.test", f"token {TEA_TOKEN}"),
        ("https://TEA.test:443", f"token {TEA_TOKEN}"),
        ("fj.test", None),
        ("codeberg.org", None),
        ("configured.test", None),
        ("oauth.test", None),
    ],
)
def test_token_goes_only_to_the_exact_login_authority(
    host: str, authorization: str | None
) -> None:
    transport = Recorder()
    with ForgejoClient(host, transport=transport) as client:
        client.object("repos", "o", "r")
    [request] = transport.requests
    assert request.headers.get("Authorization") == authorization
    assert request.url.scheme == "https"


@pytest.mark.parametrize(
    "host",
    [
        "unknown.test",
        "plain.test",
        "fj.test:3444",
        "user:pw@tea.test",
        "https://alice@tea.test",
        "tea.test/evil",
    ],
)
def test_unowned_or_credentialed_hosts_are_refused_before_any_request(host: str) -> None:
    with pytest.raises(ForgejoError):
        ForgejoClient(host, transport=Recorder())


def test_segments_cannot_change_the_request_path_or_host() -> None:
    transport = Recorder()
    with ForgejoClient("tea.test", transport=transport) as client:
        client.object("repos", "o/../x", "@evil.test", ref="a b")
    [request] = transport.requests
    assert request.url.host == "tea.test"
    assert request.url.raw_path == b"/api/v1/repos/o%2F..%2Fx/%40evil.test?ref=a+b"


def test_redirects_are_not_followed_and_carry_no_token() -> None:
    transport = Recorder(
        lambda _request: httpx.Response(302, headers={"Location": "https://evil.test/x"})
    )
    with ForgejoClient("fj.test:3443", transport=transport) as client:
        with pytest.raises(ForgejoError) as raised:
            client.object("repos", "o", "r")
    assert raised.value.status == 302
    assert [request.url.host for request in transport.requests] == ["fj.test"]


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_errors_keep_their_status_and_never_echo_the_token(status: int) -> None:
    transport = Recorder(lambda _request: httpx.Response(status, text=FJ_TOKEN))
    with ForgejoClient("fj.test:3443", transport=transport) as client:
        with pytest.raises(ForgejoError) as raised:
            client.object("repos", "o", "r")
    assert raised.value.status == status
    assert FJ_TOKEN not in str(raised.value)


@pytest.mark.parametrize("body", ["not json", "[]"])
def test_invalid_object_responses_raise(body: str) -> None:
    transport = Recorder(lambda _request: httpx.Response(200, text=body))
    with ForgejoClient("tea.test", transport=transport) as client:
        with pytest.raises(ForgejoError):
            client.object("repos", "o", "r")


@pytest.mark.parametrize("body", ["not json", "{}", "[1]"])
def test_invalid_list_responses_raise(body: str) -> None:
    transport = Recorder(lambda _request: httpx.Response(200, text=body))
    with ForgejoClient("tea.test", transport=transport) as client:
        with pytest.raises(ForgejoError):
            client.items("repos", "o", "r", "issues", 1, "comments")


def _paged(pages: list[list[dict[str, int]] | int], has_more: bool | None) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        page = pages[int(request.url.params["page"]) - 1]
        if isinstance(page, int):
            return httpx.Response(page)
        headers = {} if has_more is None else {"X-HasMore": str(has_more).lower()}
        if has_more and request.url.params["page"] == str(len(pages)):
            headers["X-HasMore"] = "false"
        return httpx.Response(200, json=page, headers=headers)

    return handle


def test_pages_follow_the_has_more_header_beyond_a_short_page() -> None:
    transport = Recorder(_paged([[{"n": 1}], [{"n": 2}]], has_more=True))
    with ForgejoClient("tea.test", transport=transport) as client:
        assert client.pages("repos", "o", "r", "pulls") == ([{"n": 1}, {"n": 2}], False)
    assert [r.url.params["limit"] for r in transport.requests] == ["50", "50"]


def test_pages_without_the_header_stop_at_a_short_page() -> None:
    full = [{"n": n} for n in range(50)]
    transport = Recorder(_paged([full, [{"n": 50}]], has_more=None))
    with ForgejoClient("tea.test", transport=transport) as client:
        values, partial = client.pages("repos", "o", "r", "pulls")
    assert (len(values), partial) == (51, False)


def test_a_failed_later_page_returns_a_partial_list() -> None:
    full = [{"n": n} for n in range(50)]
    transport = Recorder(_paged([full, 500], has_more=None))
    with ForgejoClient("tea.test", transport=transport) as client:
        values, partial = client.pages("repos", "o", "r", "pulls")
    assert (len(values), partial) == (50, True)


def test_page_limit_marks_the_list_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forgejo_client, "_MAX_PAGES", 2)
    transport = Recorder(lambda _r: httpx.Response(200, json=[{}], headers={"X-HasMore": "true"}))
    with ForgejoClient("tea.test", transport=transport) as client:
        assert client.pages("x") == ([{}, {}], True)


def test_a_request_after_the_deadline_is_not_sent() -> None:
    transport = Recorder()
    with ForgejoClient("tea.test", deadline=time.monotonic() - 1, transport=transport) as client:
        with pytest.raises(ForgejoTimeoutError):
            client.object("repos", "o", "r")
    assert transport.requests == []


def test_a_transport_timeout_is_a_timeout_error() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with ForgejoClient("tea.test", transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(ForgejoTimeoutError):
            client.text("repos", "o", "r", "pulls", "1.diff")


def test_login_changes_apply_without_restart(home: Path) -> None:
    keys = home / ".local/share/forgejo-cli/keys.json"
    keys.write_text(json.dumps({"hosts": {"new.test": {"token": "new"}}}), encoding="utf-8")
    assert logins()["new.test"] == ForgejoLogin("new.test", "new", "forgejo-cli")
    assert "fj.test:3443" not in logins()
