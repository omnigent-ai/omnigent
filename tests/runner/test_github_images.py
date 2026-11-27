"""Pull request attachment images: which URLs are shown, where the token goes, what is refused."""

from __future__ import annotations

import asyncio
import gzip
import logging
import ssl
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpcore
import httpx
import pytest

from omnigent.inner.async_utils import run_sync_on_thread
from omnigent.runner import github_images
from omnigent.runner import github_resource as github
from omnigent.runner.github_images import ImageRefused, checked_image_url, read_pull_request_image
from omnigent.runner.session_prs import PullRequestRef
from omnigent.util import tls
from tests._helpers.https_server import enable_https

TOKEN = "ghp_example_token_value"
OTHER_TOKEN = "ghp_other_example_token"
PR_URL = "https://github.com/example-org/example-repo/pull/7"
GHES = "github.example.com"
GHES_PR_URL = f"https://{GHES}/example-org/example-repo/pull/7"
GHES_ASSET = f"https://{GHES}/user-attachments/assets/0f1e2d3c-aaaa"
ASSET = "https://github.com/user-attachments/assets/0f1e2d3c-aaaa-bbbb-cccc-000000000001"
SIGNED = "https://private-user-images.githubusercontent.com/1/2-x.png?jwt=eyJ.secret.sig"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64
AVIF = b"\x00\x00\x00\x1cftypavif" + b"\x00" * 64
# An AVIF whose major brand is the generic image file format, naming AVIF as compatible.
MIF1_AVIF = b"\x00\x00\x00\x1cftypmif1\x00\x00\x00\x00mif1avifmiaf" + b"\x00" * 64

# gh's own token lookup, before a fake replaces it.
_REAL_ACTIVE_TOKEN = github._gh_active_token


class FakeGitHub:
    """Stands in for ``gh``: its account and token lookups, recording each call it runs."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Each ``gh`` call: its arguments and the token it ran with.
        self.calls: list[tuple[list[str], str | None]] = []
        monkeypatch.setattr(github, "_gh", self._gh)
        monkeypatch.setattr(github, "_host_args", lambda _root, ref: ["--hostname", ref.host])
        monkeypatch.setattr(github, "_in_sandbox", lambda: False)
        monkeypatch.setattr(github._config, "github_account_preference", lambda _repo: None)
        self.active_tokens = [TOKEN]
        # Each active-account token lookup, as binding the PR's account makes one.
        self.token_lookups: list[str] = []
        # What a sandbox's gh answers from its hosts.yml, and how that lookup ends.
        self.broker_token = ""
        self.broker_exit_code = 0
        self.broker_times_out = False
        monkeypatch.setattr(github, "_gh_active_token", self._active_token)

    def _active_token(self, _root: str, host: str = "github.com") -> str | None:
        self.token_lookups.append(host)
        return self.active_tokens.pop(0) if len(self.active_tokens) > 1 else self.active_tokens[0]

    def _gh(
        self, argv: list[str], *, cwd: str, token: str | None = None
    ) -> tuple[int | None, str, str]:
        self.calls.append((argv, token))
        if argv[:2] == ["auth", "token"]:
            if self.broker_times_out:
                github._gh_timed_out.set(True)
                return None, "", ""
            return self.broker_exit_code, f"{self.broker_token}\n", ""
        return 1, "", "An image read runs no other gh command"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No stored or configured gh hosts, no GH_HOST and every read slot free."""
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.delenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", raising=False)
    monkeypatch.setattr(github_images, "_read_slots", threading.BoundedSemaphore(4))
    yield


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    return FakeGitHub(monkeypatch)


class Transport(httpx.MockTransport):
    """Answers requests in order, and records each one."""

    def __init__(self, *responses: Callable[[httpx.Request], httpx.Response]) -> None:
        self.queue = list(responses)
        self.seen: list[httpx.Request] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.queue.pop(0)(request)


def _image(content_type: str = "image/png", data: bytes = PNG, **headers: str) -> Any:
    return lambda _request: httpx.Response(
        200, headers={"content-type": content_type, **headers}, content=data
    )


def _redirect(location: str, **headers: str) -> Any:
    return lambda _request: httpx.Response(302, headers={"location": location, **headers})


def _read(
    url: str = ASSET, transport: httpx.BaseTransport | None = None, pr_url: str = PR_URL, **kw: Any
) -> github_images.PullRequestImage:
    return read_pull_request_image(
        "/workspace", PullRequestRef.from_url(pr_url), url, transport=transport, **kw
    )


def _refusal(call: Callable[[], object]) -> str | None:
    with pytest.raises(ImageRefused) as refused:
        call()
    return refused.value.reason


# The allowlist.


@pytest.mark.parametrize("url, pr_host", [(ASSET, "github.com"), (GHES_ASSET, GHES)])
def test_an_attachment_on_the_pull_requests_own_host_is_allowed(url: str, pr_host: str) -> None:
    assert str(checked_image_url(url, pr_host)) == url


@pytest.mark.parametrize(
    "url, pr_host",
    [
        ("https://github.com.example.net/user-attachments/assets/0f1e", "github.com"),
        (f"https://evil{GHES}/user-attachments/assets/0f1e", GHES),
        (f"https://{GHES}.example.net/user-attachments/assets/0f1e", GHES),
        ("https://github.com/example-org/example-repo/issues/1.png", "github.com"),
        ("https://github.com/user-attachments/assets/0f1e/extra", "github.com"),
        ("https://github.com/user-attachments/assets/0f1e?x=1", "github.com"),
        ("https://github.com/user-attachments/assets/0F1E", "github.com"),
        ("https://raw.githubusercontent.com/a.png", "github.com"),
        ("https://objects.githubusercontent.com/a.png", "github.com"),
        ("https://192.0.2.1/user-attachments/assets/0f1e", "192.0.2.1"),
        ("https://[2001:db8::1]/user-attachments/assets/0f1e", "2001:db8::1"),
        ("https://xn--github-9ta.example/user-attachments/assets/0f1e", "xn--github-9ta.example"),
        ("https://github.com:8443/user-attachments/assets/0f1e", "github.com"),
        ("https://user@github.com/user-attachments/assets/0f1e", "github.com"),
        ("http://github.com/user-attachments/assets/0f1e", "github.com"),
        ("https://github.com./user-attachments/assets/0f1e", "github.com"),
        ("https://GitHub.com/user-attachments/assets/0f1e", "github.com"),
        (ASSET, GHES),
        (GHES_ASSET, "github.com"),
        (f"https://{GHES}/example-org/example-repo/raw/main/a.png", GHES),
        # Where an attachment redirects, and older link forms: none is fetched as asked.
        (SIGNED, "github.com"),
        ("https://user-images.githubusercontent.com/1/2-x.png", "github.com"),
        (f"https://{GHES}/storage/user/1/files/a.png", GHES),
        (f"https://media.{GHES}/user/1/a.png", GHES),
    ],
)
def test_anything_but_an_attachment_on_the_pull_requests_host_is_refused(
    url: str, pr_host: str
) -> None:
    with pytest.raises(ImageRefused, match="GitHub won't show images from that address"):
        checked_image_url(url, pr_host)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/user-attachments/assets/%2e%2e/0f1e",
        "https://github.com/user-attachments/assets/0f1e%2F..%2Fa",
        "https://github.com/user-attachments/assets/0f1e%5ca",
        "https://github.com/user-attachments/assets\\0f1e",
        "https://github.com/user-attachments/./assets/0f1e",
        "https://github.com/user-attachments/../assets/0f1e",
        "https://github.com/user-attachments/assets/0f1e 1",
        "https://github.com/user-attachments/assets/0f1e\t1",
        "https://github.com/user-attachments/assets/0f1e\x00",
        "https://user-images.githubusercontent.com/1/2-x.png",
    ],
)
def test_a_refused_url_reads_no_token_and_fetches_nothing(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    def no_token_read(_root: str, _reference: PullRequestRef) -> str | None:
        raise AssertionError("The token was read before the URL was refused")

    monkeypatch.setattr(github_images, "_image_token", no_token_read)
    transport = Transport()

    assert _refusal(lambda: _read(url, transport)) == "host_not_allowed"

    assert fake.calls == []
    assert fake.token_lookups == []
    assert transport.seen == []


def test_a_fragment_is_dropped_before_the_check_and_never_fetched(fake: FakeGitHub) -> None:
    transport = Transport(_image())

    assert _read(f"{ASSET}#preview", transport).data == PNG

    assert str(transport.seen[0].url) == ASSET


# Redirects and where the token goes.


def test_the_token_goes_only_on_the_first_request_to_the_pr_host(fake: FakeGitHub) -> None:
    transport = Transport(_redirect(SIGNED), _image())

    assert _read(ASSET, transport).data == PNG

    first, second = transport.seen
    assert first.headers["authorization"] == f"token {TOKEN}"
    assert first.headers["accept-encoding"] == "identity"
    assert str(second.url) == SIGNED
    assert "authorization" not in second.headers
    assert fake.calls == []


def test_a_signed_url_whose_query_carries_encoded_slashes_is_followed(
    fake: FakeGitHub,
) -> None:
    signed = SIGNED + "&X-Amz-Credential=key%2F20261127%2Fus-east-1%2Fs3%2Faws4_request"
    transport = Transport(_redirect(signed), _image())

    assert _read(ASSET, transport).data == PNG
    assert str(transport.seen[1].url) == signed


@pytest.mark.parametrize(
    "location",
    [f"https://media.{GHES}/user/1/a.png?token=a", f"https://{GHES}/storage/user/1/files/a.png"],
    ids=["media-host", "storage-path"],
)
def test_an_enterprise_attachment_redirects_to_its_hosts_signed_images(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    monkeypatch.setenv("GH_HOST", GHES)
    transport = Transport(_redirect(location), _image())

    assert _read(GHES_ASSET, transport, pr_url=GHES_PR_URL).data == PNG

    assert str(transport.seen[1].url) == location
    assert "authorization" not in transport.seen[1].headers


@pytest.mark.parametrize(
    "location",
    [
        "https://example.net/a.png",
        "http://private-user-images.githubusercontent.com/1/2-x.png",
        "https://user-images.githubusercontent.com/1/2-x.png",
        "https://github.com/storage/user/1/a.png",
        f"https://media.{GHES}/user/1/a.png",
        "https://raw.githubusercontent.com/a.png",
        "/example-org/example-repo/issues/1",
        # Back to github.com: another asset, or the same one with a query.
        "https://github.com/user-attachments/assets/0f1e2d3c-aaaa-bbbb-cccc-000000000002",
        "?jwt=a",
        # Each resolves to an allowed URL; the raw reference is what the rules refuse.
        "https://private-user-images.githubusercontent.com/1/../2-x.png?jwt=a",
        "https://private-user-images.githubusercontent.com/1/./2-x.png?jwt=a",
        "https://private-user-images.githubusercontent.com/1/2 x.png?jwt=a",
        "https://private-user-images.githubusercontent.com/1/%2e%2e/2-x.png?jwt=a",
        "https://private-user-images.githubusercontent.com/1/2\tx.png?jwt=a",
        "//evil.example/a.png",
        "https://private-user-images.githubusercontent.com:8443/1/2-x.png",
        "https://user@private-user-images.githubusercontent.com/1/2-x.png",
        # An absolute location is held to the rules as written, case and port included.
        "https://PRIVATE-USER-IMAGES.githubusercontent.com/1/2-x.png?jwt=a",
        "https://private-user-images.githubusercontent.com:443/1/2-x.png?jwt=a",
        # So is a scheme-relative one, which names its host too.
        "//PRIVATE-USER-IMAGES.githubusercontent.com/1/2-x.png?jwt=a",
        "//private-user-images.githubusercontent.com:443/1/2-x.png?jwt=a",
    ],
)
def test_a_redirect_off_the_signed_image_host_is_refused_with_no_request_to_it(
    fake: FakeGitHub, location: str
) -> None:
    transport = Transport(_redirect(location))

    assert _refusal(lambda: _read(ASSET, transport)) == "host_not_allowed"

    assert len(transport.seen) == 1


def test_a_relative_redirect_on_the_signed_image_host_is_followed_without_the_token(
    fake: FakeGitHub,
) -> None:
    transport = Transport(_redirect(SIGNED), _redirect("/1/3-x.png?jwt=b"), _image())

    assert _read(ASSET, transport).data == PNG

    followed = transport.seen[2]
    assert str(followed.url) == "https://private-user-images.githubusercontent.com/1/3-x.png?jwt=b"
    assert "authorization" not in followed.headers


def test_an_enterprise_redirect_to_github_coms_signed_images_is_refused(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_HOST", GHES)
    transport = Transport(_redirect(SIGNED))

    assert _refusal(lambda: _read(GHES_ASSET, transport, pr_url=GHES_PR_URL)) == (
        "host_not_allowed"
    )
    assert len(transport.seen) == 1


def test_three_redirects_are_followed_and_a_fourth_is_refused(fake: FakeGitHub) -> None:
    hops = [_redirect(SIGNED)] * 3
    assert _read(ASSET, Transport(*hops, _image())).data == PNG

    transport = Transport(*[_redirect(SIGNED)] * 4)
    assert _refusal(lambda: _read(ASSET, transport)) == "fetch_failed"
    assert len(transport.seen) == 4


def test_a_redirect_without_a_location_fails(fake: FakeGitHub) -> None:
    transport = Transport(lambda _request: httpx.Response(302))

    assert _refusal(lambda: _read(ASSET, transport)) == "fetch_failed"


def test_a_cookie_from_one_hop_isnt_sent_on_the_next(fake: FakeGitHub) -> None:
    transport = Transport(_redirect(SIGNED, **{"set-cookie": "session=abc; Path=/"}), _image())

    _read(ASSET, transport)

    assert "cookie" not in transport.seen[1].headers


# Size, time and type.


def test_a_declared_size_over_the_cap_is_refused_unread(fake: FakeGitHub) -> None:
    too_large = str(github_images.MAX_IMAGE_BYTES + 1)

    def declared(_request: httpx.Request) -> httpx.Response:
        async def never_read() -> AsyncIterator[bytes]:
            pytest.fail("the body must not be read")
            yield b""

        headers = {"content-type": "image/png", "content-length": too_large}
        return httpx.Response(200, headers=headers, content=never_read())

    assert _refusal(lambda: _read(ASSET, Transport(declared))) == "too_large"


def test_a_stream_past_the_cap_stops_there(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_images, "MAX_IMAGE_BYTES", 1000)
    sent: list[int] = []

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(100):
            sent.append(1)
            yield PNG[:8] + b"\x00" * 392

    transport = Transport(
        lambda _request: httpx.Response(
            200, headers={"content-type": "image/png"}, content=chunks()
        )
    )

    assert _refusal(lambda: _read(ASSET, transport)) == "too_large"
    assert len(sent) == 3


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "gzip, identity"])
def test_a_compressed_answer_is_refused_before_it_is_decoded(
    fake: FakeGitHub, encoding: str
) -> None:
    bomb = gzip.compress(PNG + b"\x00" * 1_000_000)
    read: list[bytes] = []

    async def body() -> AsyncIterator[bytes]:
        read.append(bomb)
        yield bomb

    headers = {"content-type": "image/png", "content-encoding": encoding}
    transport = Transport(lambda _request: httpx.Response(200, headers=headers, content=body()))

    assert _refusal(lambda: _read(ASSET, transport)) == "not_image"
    assert read == []


def _after(now: list[float], seconds: float, answer: Any) -> Any:
    """An answer that takes ``seconds`` of the fake clock to arrive."""

    def answer_later(request: httpx.Request) -> httpx.Response:
        now[0] += seconds
        return answer(request)

    return answer_later


def test_a_redirect_hop_past_the_deadline_is_never_sent(fake: FakeGitHub) -> None:
    now = [0.0]
    transport = Transport(
        _after(now, 20.0, _redirect(SIGNED)), _after(now, 20.0, _redirect(SIGNED)), _image()
    )

    assert _refusal(lambda: _read(ASSET, transport, clock=lambda: now[0])) == "fetch_failed"
    assert len(transport.seen) == 2


def test_each_hop_waits_no_longer_than_the_time_left(fake: FakeGitHub) -> None:
    now = [0.0]
    transport = Transport(_after(now, 25.0, _redirect(SIGNED)), _image())

    _read(ASSET, transport, clock=lambda: now[0])

    first, second = (request.extensions["timeout"] for request in transport.seen)
    assert set(first.values()) == {10.0}
    assert set(second.values()) == {5.0}


def test_a_server_sending_past_the_deadline_is_cut_off(fake: FakeGitHub) -> None:
    now = [0.0]

    async def drip() -> AsyncIterator[bytes]:
        yield PNG[:8]
        for _ in range(10):
            now[0] += 10.0
            yield b"\x00"

    transport = Transport(
        lambda _request: httpx.Response(200, headers={"content-type": "image/png"}, content=drip())
    )

    assert _refusal(lambda: _read(ASSET, transport, clock=lambda: now[0])) == "fetch_failed"
    assert now[0] <= github_images.FETCH_DEADLINE_SECONDS + 10.0


def _slow(header_seconds: float, *, status: int = 200, body_seconds: float = 0.0) -> Any:
    """An answer whose headers, then its one body part, each take real time to arrive."""
    headers = {"location": SIGNED} if status == 302 else {"content-type": "image/png"}

    async def answer(_request: httpx.Request) -> httpx.Response:
        async def body() -> AsyncIterator[bytes]:
            await asyncio.sleep(body_seconds)
            yield PNG

        await asyncio.sleep(header_seconds)
        return httpx.Response(status, headers=headers, content=body())

    return answer


async def test_a_read_from_the_hosts_worker_thread_runs_its_own_event_loop(
    fake: FakeGitHub,
) -> None:
    """The host calls the read from its event loop, through a worker thread."""
    image = await run_sync_on_thread(_read, ASSET, Transport(_image()))

    assert image.data == PNG


def test_the_whole_fetch_ends_at_its_deadline_though_no_wait_passes_its_cap(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each wait is under its hop's cap, but together they would run 0.8 s past the deadline."""
    monkeypatch.setattr(github_images, "FETCH_DEADLINE_SECONDS", 2.0)
    # The redirect takes 1 s of the 2; the image's headers and its body take 0.9 s each.
    transport = Transport(_slow(1.0, status=302), _slow(0.9, body_seconds=0.9))
    started = time.monotonic()

    assert _refusal(lambda: _read(ASSET, transport)) == "fetch_failed"

    assert time.monotonic() - started < 2.4


@pytest.mark.parametrize(
    "content_type, data, headers",
    [
        ("image/png", PNG, {}),
        ("image/jpeg", JPEG, {}),
        ("image/gif", GIF, {}),
        ("image/webp", WEBP, {}),
        ("image/avif", AVIF, {}),
        ("image/avif", MIF1_AVIF, {}),
        ("image/PNG; charset=binary", PNG, {}),
        ("image/png", PNG, {"content-encoding": "identity"}),
    ],
)
def test_each_raster_type_with_matching_bytes_is_served(
    fake: FakeGitHub, content_type: str, data: bytes, headers: dict[str, str]
) -> None:
    image = _read(ASSET, Transport(_image(content_type, data, **headers)))

    assert (image.content_type, image.data) == (content_type.split(";")[0].lower(), data)


@pytest.mark.parametrize(
    "content_type, data",
    [
        ("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>"),
        ("text/html", b"<html><script></script></html>"),
        ("image/png", JPEG),
        ("image/jpeg", b"<svg/>"),
        ("text/html; charset=image/png", PNG),
        ("", PNG),
        # An image file format box naming no AVIF brand, then AVIF past the box's bounds.
        ("image/avif", b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1heic" + b"\x00" * 64),
        ("image/avif", b"\x00\x00\x00\x14ftypmif1\x00\x00\x00\x00mif1avif" + b"\x00" * 64),
        ("image/avif", b"\x00\x00\x00\x20ftypmif1\x00\x00\x00\x00mif1avif"),
    ],
)
def test_anything_but_a_matching_raster_image_is_refused(
    fake: FakeGitHub, content_type: str, data: bytes
) -> None:
    assert _refusal(lambda: _read(ASSET, Transport(_image(content_type, data)))) == "not_image"


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_an_error_answer_fails(fake: FakeGitHub, status: int) -> None:
    transport = Transport(lambda _request: httpx.Response(status))

    assert _refusal(lambda: _read(ASSET, transport)) == "fetch_failed"


@pytest.mark.parametrize(
    "error",
    [
        ssl.SSLError(1, f"certificate verify failed for {SIGNED}"),
        httpx.ConnectError(f"connect to {SIGNED} failed"),
        httpx.ReadError(f"read from {SIGNED} failed"),
        httpx.RemoteProtocolError(f"bad answer from {SIGNED}"),
        OSError(f"socket error for {SIGNED}"),
    ],
    ids=["tls", "connect", "read", "protocol", "socket"],
)
def test_a_transport_error_fails_without_naming_the_url_or_token(
    fake: FakeGitHub, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    def fail(_request: httpx.Request) -> httpx.Response:
        raise error

    caplog.set_level(logging.DEBUG)
    with pytest.raises(ImageRefused) as refused:
        _read(ASSET, Transport(_redirect(SIGNED), fail))

    assert refused.value.reason == "fetch_failed"
    assert refused.value.__cause__ is None
    assert refused.value.__suppress_context__
    shown = str(refused.value) + caplog.text
    for secret in ("secret", TOKEN, "user-attachments", "githubusercontent"):
        assert secret not in shown


def test_every_refusal_reads_one_fixed_message() -> None:
    messages = {
        reason: str(ImageRefused(reason)) for reason in github_images.IMAGE_REFUSAL_REASONS
    }

    assert len(set(messages.values())) == 4
    assert all("http" not in message for message in messages.values())


# The real transport, over a fake network: what tracing and logging see.


class _RecordingStream(httpcore.AsyncMockStream):
    """One connection's answer, recording the bytes the request writes."""

    def __init__(self, answer: bytes, written: list[bytes]) -> None:
        super().__init__([answer])
        self._written = written

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._written.append(buffer)


class _Network(httpcore.AsyncMockBackend):
    """Answers each new connection with the next raw HTTP response."""

    def __init__(self, *answers: bytes) -> None:
        super().__init__([])
        self.answers = list(answers)
        self.written: list[bytes] = []

    async def connect_tcp(self, *_args: Any, **_kwargs: Any) -> httpcore.AsyncNetworkStream:
        return _RecordingStream(self.answers.pop(0), self.written)


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> _Network:
    """Every hop gets httpx's own transport, over a network that answers a signed redirect."""
    for variable in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(variable, raising=False)
    image = b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: %d\r\n\r\n" % len(PNG)
    network = _Network(
        f"HTTP/1.1 302 Found\r\nLocation: {SIGNED}\r\nContent-Length: 0\r\n\r\n".encode(),
        image + PNG,
    )
    real = github_images._transport_for

    def transport_for(url: httpx.URL) -> httpx.AsyncHTTPTransport:
        transport = real(url)
        transport._pool = httpcore.AsyncConnectionPool(network_backend=network)
        return transport

    monkeypatch.setattr(github_images, "_transport_for", transport_for)
    return network


def test_a_fetch_emits_no_span_and_sends_no_trace_header(
    fake: FakeGitHub, network: _Network
) -> None:
    """A client span would record each hop's URL, and the signed one is a credential."""
    instrumentation = pytest.importorskip("opentelemetry.instrumentation.httpx")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exported = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exported))
    instrumentor = instrumentation.HTTPXClientInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        assert _read(ASSET).data == PNG
    finally:
        instrumentor.uninstrument()

    assert exported.get_finished_spans() == ()
    sent = b"".join(network.written)
    assert b"GET /1/2-x.png?jwt=" in sent
    assert b"traceparent" not in sent.lower()


def test_a_fetch_leaves_no_url_in_the_transports_debug_records(
    fake: FakeGitHub, network: _Network, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    assert _read(ASSET).data == PNG

    assert "githubusercontent" not in caplog.text
    assert "user-attachments" not in caplog.text


def test_other_requests_keep_their_transport_records(
    network: _Network, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def fetch() -> None:
        async with github_images._transport_for(httpx.URL(SIGNED)) as transport:
            response = await transport.handle_async_request(httpx.Request("GET", SIGNED))
            await response.aclose()

    asyncio.run(fetch())

    assert "receive_response_headers" in caplog.text


@pytest.mark.parametrize(
    "proxy",
    [
        "gopher://proxy-user@proxy.example:70",
        "http://proxy-user@proxy.example:not-a-port",
        "socks5://proxy-user@proxy.example:1080",
    ],
    ids=["unknown-scheme", "invalid-url", "socks-without-socksio"],
)
def test_a_proxy_httpx_cant_use_fails_the_fetch_without_its_diagnostic(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, proxy: str
) -> None:
    """An unusable HTTPS_PROXY breaks the transport before any request is made."""
    for variable in ("https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    # SOCKS needs the optional socksio package; without it httpx can't build the proxy.
    monkeypatch.setitem(sys.modules, "socksio", None)

    with pytest.raises(ImageRefused) as refused:
        _read(ASSET)

    assert refused.value.reason == "fetch_failed"
    assert str(refused.value) == "GitHub couldn't load that image. Try again."
    assert refused.value.__cause__ is None


@pytest.mark.parametrize(
    ("no_proxy", "expected"),
    [("", "http://proxy.example:3128"), ("githubusercontent.com", None)],
    ids=["proxied", "no-proxy-match"],
)
def test_the_fetch_uses_https_proxy_unless_no_proxy_names_the_host(
    monkeypatch: pytest.MonkeyPatch, no_proxy: str, expected: str | None
) -> None:
    for variable in ("https_proxy", "no_proxy"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", no_proxy)
    built: list[dict[str, Any]] = []
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: built.append(kwargs))

    github_images._transport_for(httpx.URL(SIGNED))

    [kwargs] = built
    assert kwargs["proxy"] == expected
    assert kwargs["trust_env"] is False


# Slots.


def test_a_read_waits_for_a_free_slot(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_images, "SLOT_WAIT_SECONDS", 5.0)
    slots = github_images._read_slots
    for _ in range(github_images.MAX_CONCURRENT_READS):
        slots.acquire()
    threading.Timer(0.05, slots.release).start()

    assert _read(ASSET, Transport(_image())).data == PNG


def test_a_read_with_no_slot_free_in_time_fails_with_nothing_fetched(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_images, "SLOT_WAIT_SECONDS", 0.01)
    for _ in range(github_images.MAX_CONCURRENT_READS):
        github_images._read_slots.acquire()
    transport = Transport()

    assert _refusal(lambda: _read(ASSET, transport)) == "fetch_failed"
    assert transport.seen == []


def test_a_failed_read_frees_its_slot(fake: FakeGitHub) -> None:
    for _ in range(github_images.MAX_CONCURRENT_READS + 1):
        _refusal(lambda: _read(ASSET, Transport(lambda _request: httpx.Response(500))))

    assert _read(ASSET, Transport(_image())).data == PNG


# The account and the token's source.


def test_the_fetch_runs_as_the_account_active_at_the_start(fake: FakeGitHub) -> None:
    """Switching gh's active account mid-read must not change whose token fetches."""
    fake.active_tokens = [TOKEN, OTHER_TOKEN]
    transport = Transport(_image())

    _read(ASSET, transport)

    assert transport.seen[0].headers["authorization"] == f"token {TOKEN}"
    assert fake.calls == []


def test_the_fetch_runs_as_the_account_saved_for_the_repository(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A login chosen for the repository fetches with its own token, not the active one."""
    monkeypatch.setattr(github._config, "github_account_preference", lambda _repo: "saved-login")
    fake.broker_token = "gho_saved_example"
    transport = Transport(_image())

    _read(ASSET, transport)

    assert transport.seen[0].headers["authorization"] == "token gho_saved_example"
    assert [argv for argv, _token in fake.calls] == [
        ["auth", "token", "--user", "saved-login", "-h", "github.com"]
    ]
    assert fake.token_lookups == []


def test_a_pr_on_a_host_the_user_never_set_up_reads_nothing_and_sends_no_token(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With only an enterprise token in the environment, an unknown host gets nothing."""
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", TOKEN)
    lookups: list[str] = []
    monkeypatch.setattr(github, "_gh_active_token", lambda _root, host: lookups.append(host))
    transport = Transport()

    with pytest.raises(ValueError, match="Sign in to this GitHub host with gh"):
        _read(GHES_ASSET, transport, pr_url=GHES_PR_URL)

    assert fake.calls == []
    assert lookups == []
    assert transport.seen == []


def test_a_pr_on_the_gh_host_set_up_is_read(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_HOST", GHES)
    transport = Transport(_image())

    assert _read(GHES_ASSET, transport, pr_url=GHES_PR_URL).data == PNG
    assert transport.seen[0].headers["authorization"] == f"token {TOKEN}"


@pytest.mark.parametrize(
    ("exit_code", "output", "times_out", "message"),
    [
        (1, "gho_brokered_example", False, "Couldn't read the host's GitHub sign-in"),
        (0, "", False, "Couldn't read the host's GitHub sign-in"),
        (0, "", True, "GitHub authentication timed out"),
    ],
    ids=["failed", "empty", "timed-out"],
)
def test_a_sandbox_whose_token_cant_be_read_fetches_nothing(
    fake: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    output: str,
    times_out: bool,
    message: str,
) -> None:
    """Without the owner's token the fetch would run signed out, so it never starts."""
    monkeypatch.setattr(github, "_in_sandbox", lambda: True)
    monkeypatch.setattr(github, "_gh_active_token", _REAL_ACTIVE_TOKEN)
    fake.broker_token = output
    fake.broker_exit_code = exit_code
    fake.broker_times_out = times_out
    transport = Transport(_image())

    with pytest.raises(ValueError, match=message):
        _read(ASSET, transport)

    assert transport.seen == []


def test_an_empty_bound_token_fetches_nothing(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty token would send a bare Authorization header, so the read is refused."""
    monkeypatch.setattr(github, "_pr_token", lambda _root, _reference: "")
    transport = Transport(_image())

    with pytest.raises(ValueError, match="Couldn't read the host's GitHub sign-in"):
        _read(ASSET, transport)

    assert transport.seen == []


def test_a_sandbox_fetches_with_its_brokered_token_on_the_first_request_only(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sandbox's gh signs in from hosts.yml; the fetch asks gh for that token, not the env."""
    monkeypatch.setattr(github, "_in_sandbox", lambda: True)
    monkeypatch.setenv("GH_TOKEN", OTHER_TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", OTHER_TOKEN)
    monkeypatch.setattr(github, "_gh_active_token", _REAL_ACTIVE_TOKEN)
    fake.broker_token = "gho_brokered_example"
    transport = Transport(_redirect(SIGNED), _image())

    assert _read(ASSET, transport).data == PNG

    first, second = transport.seen
    assert first.headers["authorization"] == "token gho_brokered_example"
    assert "authorization" not in second.headers
    assert ["auth", "token", "-h", "github.com"] in [argv for argv, _token in fake.calls]


# TLS.


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[tuple[int, str]]:
    """A local HTTPS server: its port and the certificate authority that signed it."""
    import http.server

    class Answer(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("content-type", "image/png")
            self.send_header("content-length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Answer)
    trust = enable_https(server, tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], trust["SSL_CERT_FILE"]
    server.shutdown()
    server.server_close()


def _fetch_from(port: int) -> int:
    url = httpx.URL(f"https://localhost:{port}/a.png")

    async def fetch() -> int:
        async with github_images._transport_for(url) as transport:
            response = await transport.handle_async_request(httpx.Request("GET", url))
            await response.aclose()
            return response.status_code

    return asyncio.run(fetch())


def test_a_server_with_an_untrusted_certificate_is_refused(
    monkeypatch: pytest.MonkeyPatch, tls_server: tuple[int, str]
) -> None:
    port, _authority = tls_server
    monkeypatch.setattr(tls, "_client_ssl_context", None)
    for variable in ("HTTPS_PROXY", "https_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(variable, raising=False)

    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        _fetch_from(port)


def test_a_certificate_authority_in_ssl_cert_file_is_trusted(
    monkeypatch: pytest.MonkeyPatch, tls_server: tuple[int, str]
) -> None:
    port, authority = tls_server
    monkeypatch.setattr(tls, "_client_ssl_context", None)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", authority)

    assert _fetch_from(port) == 200
