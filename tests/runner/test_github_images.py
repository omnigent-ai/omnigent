"""Pull request images: which URLs are shown, where the token goes, and what is refused."""

from __future__ import annotations

import datetime
import gzip
import json
import logging
import re
import ssl
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from omnigent.runner import github_images
from omnigent.runner import github_resource as github
from omnigent.runner.github_images import ImageRefused, checked_image_url, read_pull_request_image
from omnigent.runner.session_prs import PullRequestRef
from omnigent.util import tls

TOKEN = "ghp_example_token_value"
OTHER_TOKEN = "ghp_other_example_token"
PR_URL = "https://github.com/example-org/example-repo/pull/7"
GHES = "github.example.com"
GHES_PR_URL = f"https://{GHES}/example-org/example-repo/pull/7"
ASSET = "https://github.com/user-attachments/assets/0f1e2d3c-aaaa-bbbb-cccc-000000000001"
NEWER_ASSET = "https://github.com/user-attachments/assets/0f1e2d3c-aaaa-bbbb-cccc-000000000002"
SIGNED = "https://private-user-images.githubusercontent.com/1/2-x.png?jwt=eyJ.secret.sig"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64
AVIF = b"\x00\x00\x00\x1cftypavif" + b"\x00" * 64
LISTINGS = ("issues/*/comments", "pulls/*/comments", "pulls/*/reviews")
PAGE_SIZE = 100


class FakeGitHub:
    """Answers ``gh api`` reads of one pull request and its lists, recording each call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.pull: dict[str, Any] = {
            "body": f"Screenshot: ![s]({ASSET})",
            "head": {"sha": "a" * 40},
            "updated_at": "2026-11-22T10:00:00Z",
        }
        self.lists: dict[str, list[str]] = {listing: [] for listing in LISTINGS}
        # Each ``gh`` call: its arguments and the token it ran with.
        self.calls: list[tuple[list[str], str | None]] = []
        monkeypatch.setattr(github, "_gh", self._gh)
        monkeypatch.setattr(github, "_host_args", lambda _root, ref: ["--hostname", ref.host])
        monkeypatch.setattr(github, "_in_sandbox", lambda: False)
        monkeypatch.setattr(github._config, "github_account_preference", lambda _repo: None)
        self.active_tokens = [TOKEN]
        monkeypatch.setattr(github, "_gh_active_token", self._active_token)

    def _active_token(self, _root: str, _host: str = "github.com") -> str | None:
        return self.active_tokens.pop(0) if len(self.active_tokens) > 1 else self.active_tokens[0]

    def _gh(
        self, argv: list[str], *, cwd: str, token: str | None = None
    ) -> tuple[int | None, str, str]:
        self.calls.append((argv, token))
        endpoint = argv[-1]
        path, _, query = endpoint.partition("?")
        if not query:
            return 0, json.dumps(self.pull), ""
        key = re.sub(r"^repos/[^/]+/[^/]+/", "", re.sub(r"/\d+/", "/*/", path))
        page = int(parse_qs(query)["page"][0])
        bodies = self.lists[key][(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        return 0, json.dumps([{"body": body} for body in bodies]), ""

    def endpoints(self) -> list[str]:
        return [argv[-1] for argv, _token in self.calls]


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No stored gh hosts, no GH_HOST, a fresh cache and every read slot free."""
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.setattr(github_images, "_read_slots", threading.BoundedSemaphore(4))
    github_images._linked_urls_cache.clear()
    yield
    github_images._linked_urls_cache.clear()


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


@pytest.mark.parametrize(
    "url, pr_host",
    [
        (ASSET, "github.com"),
        (SIGNED, "github.com"),
        ("https://user-images.githubusercontent.com/1/2-x.png", "github.com"),
        (f"https://{GHES}/user-attachments/assets/0f1e2d3c-aaaa", GHES),
        (f"https://{GHES}/storage/user/1/files/a.png", GHES),
        (f"https://media.{GHES}/user/1/a.png", GHES),
    ],
)
def test_an_image_on_its_pull_requests_image_hosts_is_allowed(url: str, pr_host: str) -> None:
    assert str(checked_image_url(url, pr_host)) == url


@pytest.mark.parametrize(
    "url, pr_host",
    [
        ("https://github.com.example.net/user-attachments/assets/0f1e", "github.com"),
        ("https://evilgithub.com/user-attachments/assets/0f1e", "github.com"),
        ("https://github.com/example-org/example-repo/issues/1.png", "github.com"),
        ("https://github.com/user-attachments/assets/0f1e/extra", "github.com"),
        ("https://raw.githubusercontent.com/a.png", "github.com"),
        ("https://objects.githubusercontent.com/a.png", "github.com"),
        ("https://x.user-images.githubusercontent.com/a.png", "github.com"),
        ("https://140.82.112.3/user-attachments/assets/0f1e", "github.com"),
        ("https://[::1]/user-attachments/assets/0f1e", "github.com"),
        ("https://xn--github-9ta.com/user-attachments/assets/0f1e", "github.com"),
        ("https://user-images.githubusercontent.com:8443/a.png", "github.com"),
        ("https://user@user-images.githubusercontent.com/a.png", "github.com"),
        ("http://user-images.githubusercontent.com/a.png", "github.com"),
        ("https://user-images.githubusercontent.com./a.png", "github.com"),
        ("https://GitHub.com/user-attachments/assets/0f1e", "github.com"),
        ("https://github.com/storage/user/1/a.png", "github.com"),
        (f"https://media.{GHES}/a.png", "github.com"),
        (ASSET, GHES),
        (SIGNED, GHES),
        (f"https://{GHES}/example-org/example-repo/raw/main/a.png", GHES),
        (f"https://other.{GHES}/storage/user/1/a.png", GHES),
    ],
)
def test_lookalikes_and_other_hosts_and_paths_are_refused(url: str, pr_host: str) -> None:
    with pytest.raises(ImageRefused, match="GitHub won't show images from that address"):
        checked_image_url(url, pr_host)


@pytest.mark.parametrize(
    "url",
    [
        "https://user-images.githubusercontent.com/1/%2e%2e/a.png",
        "https://user-images.githubusercontent.com/1/%2E%2E%2Fa.png",
        "https://user-images.githubusercontent.com/1%5ca.png",
        "https://user-images.githubusercontent.com/1\\a.png",
        "https://user-images.githubusercontent.com/1/./a.png",
        "https://user-images.githubusercontent.com/1/../a.png",
        "https://user-images.githubusercontent.com/1/a b.png",
        "https://user-images.githubusercontent.com/1/a\tb.png",
        "https://user-images.githubusercontent.com/1/a\x00.png",
    ],
)
def test_encoded_and_dot_segment_paths_are_refused_before_github_is_asked(
    fake: FakeGitHub, url: str
) -> None:
    fake.pull["body"] = url
    transport = Transport()

    assert _refusal(lambda: _read(url, transport)) == "host_not_allowed"

    assert fake.calls == []
    assert transport.seen == []


def test_a_fragment_is_dropped_before_the_check_and_never_fetched(fake: FakeGitHub) -> None:
    linked = f"{ASSET}#preview"
    fake.pull["body"] = linked
    transport = Transport(_image())

    assert _read(linked, transport).data == PNG

    assert str(transport.seen[0].url) == ASSET


# Linking and the cache.


def test_an_image_the_pull_request_doesnt_link_is_refused_with_nothing_fetched(
    fake: FakeGitHub,
) -> None:
    transport = Transport()

    assert _refusal(lambda: _read(NEWER_ASSET, transport)) == "not_in_pr"

    assert transport.seen == []


@pytest.mark.parametrize("listing", LISTINGS)
def test_an_image_linked_from_any_comment_list_is_served(fake: FakeGitHub, listing: str) -> None:
    fake.pull["body"] = ""
    fake.lists[listing] = [f'<img src="{NEWER_ASSET}" alt="after">']

    assert _read(NEWER_ASSET, Transport(_image())).data == PNG


def test_an_image_linked_from_a_later_page_is_served(fake: FakeGitHub) -> None:
    fake.lists["issues/*/comments"] = ["no image"] * PAGE_SIZE + [f"See {NEWER_ASSET}."]

    assert _read(NEWER_ASSET, Transport(_image())).data == PNG
    assert "repos/example-org/example-repo/issues/7/comments?per_page=100&page=2" in (
        fake.endpoints()
    )


def test_a_second_read_of_the_same_revision_reads_the_pr_but_lists_once(fake: FakeGitHub) -> None:
    _read(ASSET, Transport(_image()))
    _read(ASSET, Transport(_image()))

    pull = "repos/example-org/example-repo/pulls/7"
    assert fake.endpoints().count(pull) == 2
    assert len(fake.endpoints()) == 2 + len(LISTINGS)


def test_a_new_revision_lists_again(fake: FakeGitHub) -> None:
    _read(ASSET, Transport(_image()))
    fake.pull["updated_at"] = "2026-11-22T11:00:00Z"
    fake.pull["body"] = f"Now: {NEWER_ASSET}"

    assert _read(NEWER_ASSET, Transport(_image())).data == PNG
    assert _refusal(lambda: _read(ASSET, Transport())) == "not_in_pr"
    assert len(github_images._linked_urls_cache) == 1


def test_a_cached_revision_lists_again_once_its_time_is_up(fake: FakeGitHub) -> None:
    now = [1000.0]
    _read(ASSET, Transport(_image()), clock=lambda: now[0])
    # An edit GitHub didn't stamp with a new update time.
    fake.pull["body"] = f"Now: {NEWER_ASSET}"

    assert _refusal(lambda: _read(NEWER_ASSET, Transport(), clock=lambda: now[0])) == "not_in_pr"
    now[0] += github_images.LINKED_URLS_TTL_SECONDS
    assert _read(NEWER_ASSET, Transport(_image()), clock=lambda: now[0]).data == PNG


def test_the_cache_keeps_a_bounded_number_of_revisions(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_images, "MAX_CACHED_REVISIONS", 2)
    for number in (7, 8, 9):
        _read(ASSET, Transport(_image()), pr_url=f"{PR_URL.rsplit('/', 1)[0]}/{number}")

    assert [key[2] for key in github_images._linked_urls_cache] == [8, 9]


# Redirects and where the token goes.


def test_the_token_goes_only_on_the_first_request_to_the_pr_host(fake: FakeGitHub) -> None:
    transport = Transport(_redirect(SIGNED), _image())

    assert _read(ASSET, transport).data == PNG

    first, second = transport.seen
    assert first.headers["authorization"] == f"token {TOKEN}"
    assert first.headers["accept-encoding"] == "identity"
    assert str(second.url) == SIGNED
    assert "authorization" not in second.headers


def test_an_image_on_another_host_is_fetched_without_the_token(fake: FakeGitHub) -> None:
    public = "https://user-images.githubusercontent.com/1/2-x.png"
    fake.pull["body"] = public
    transport = Transport(_image())

    _read(public, transport)

    assert "authorization" not in transport.seen[0].headers


def test_a_same_host_redirect_doesnt_carry_the_token(fake: FakeGitHub) -> None:
    transport = Transport(_redirect(NEWER_ASSET), _image())

    _read(ASSET, transport)

    assert "authorization" not in transport.seen[1].headers


@pytest.mark.parametrize(
    "location",
    [
        "https://example.net/a.png",
        "http://private-user-images.githubusercontent.com/1/2-x.png",
        "https://github.com/storage/user/1/a.png",
        "https://raw.githubusercontent.com/a.png",
        "/example-org/example-repo/issues/1",
    ],
)
def test_a_redirect_off_the_allowlist_is_refused_with_no_request_to_it(
    fake: FakeGitHub, location: str
) -> None:
    transport = Transport(_redirect(location))

    assert _refusal(lambda: _read(ASSET, transport)) == "host_not_allowed"

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
        def never_read() -> Iterator[bytes]:
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

    def chunks() -> Iterator[bytes]:
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


def test_a_compressed_answer_is_counted_decoded(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_images, "MAX_IMAGE_BYTES", 10_000)
    bomb = gzip.compress(PNG + b"\x00" * 1_000_000)
    transport = Transport(_image(data=bomb, **{"content-encoding": "gzip"}))

    assert _refusal(lambda: _read(ASSET, transport)) == "too_large"
    assert len(bomb) < 10_000


def test_a_server_sending_past_the_deadline_is_cut_off(fake: FakeGitHub) -> None:
    now = [0.0]

    def drip() -> Iterator[bytes]:
        yield PNG[:8]
        for _ in range(10):
            now[0] += 10.0
            yield b"\x00"

    transport = Transport(
        lambda _request: httpx.Response(200, headers={"content-type": "image/png"}, content=drip())
    )

    assert _refusal(lambda: _read(ASSET, transport, clock=lambda: now[0])) == "fetch_failed"
    assert now[0] <= github_images.FETCH_DEADLINE_SECONDS + 10.0


@pytest.mark.parametrize(
    "content_type, data",
    [
        ("image/png", PNG),
        ("image/jpeg", JPEG),
        ("image/gif", GIF),
        ("image/webp", WEBP),
        ("image/avif", AVIF),
        ("image/PNG; charset=binary", PNG),
    ],
)
def test_each_raster_type_with_matching_bytes_is_served(
    fake: FakeGitHub, content_type: str, data: bytes
) -> None:
    image = _read(ASSET, Transport(_image(content_type, data)))

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

    assert len(set(messages.values())) == 5
    assert all("http" not in message for message in messages.values())


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


def test_the_pr_read_its_lists_and_the_fetch_run_as_the_account_active_at_the_start(
    fake: FakeGitHub,
) -> None:
    """Switching gh's active account mid-read must not change whose token fetches."""
    fake.active_tokens = [TOKEN, OTHER_TOKEN]
    transport = Transport(_image())

    _read(ASSET, transport)

    assert {token for _argv, token in fake.calls} == {TOKEN}
    assert transport.seen[0].headers["authorization"] == f"token {TOKEN}"


def test_a_pr_on_a_host_the_user_never_set_up_reads_nothing_and_sends_no_token(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With only an enterprise token in the environment, an unknown host gets nothing."""
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", TOKEN)
    lookups: list[str] = []
    monkeypatch.setattr(github, "_gh_active_token", lambda _root, host: lookups.append(host))
    url = f"https://{GHES}/user-attachments/assets/0f1e2d3c-aaaa"
    transport = Transport()

    with pytest.raises(ValueError, match="Sign in to this GitHub host with gh"):
        _read(url, transport, pr_url=GHES_PR_URL)

    assert fake.calls == []
    assert lookups == []
    assert transport.seen == []


def test_a_pr_on_the_gh_host_set_up_is_read(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_HOST", GHES)
    url = f"https://{GHES}/user-attachments/assets/0f1e2d3c-aaaa"
    fake.pull["body"] = url
    transport = Transport(_image())

    assert _read(url, transport, pr_url=GHES_PR_URL).data == PNG
    assert transport.seen[0].headers["authorization"] == f"token {TOKEN}"


def test_a_sandbox_fetches_without_a_token(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "_in_sandbox", lambda: True)
    transport = Transport(_image())

    _read(ASSET, transport)

    assert "authorization" not in transport.seen[0].headers


# TLS.


def _certificate(directory: Path) -> tuple[Path, Path]:
    """A self-signed certificate for ``localhost``, and its key."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    """A local HTTPS server with a self-signed certificate: its port and certificate."""
    import http.server

    cert_path, key_path = _certificate(tmp_path)

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
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], cert_path
    server.shutdown()
    server.server_close()


def _fetch_from(port: int) -> int:
    url = httpx.URL(f"https://localhost:{port}/a.png")
    with github_images._transport_for(url) as transport:
        response = transport.handle_request(httpx.Request("GET", url))
        response.close()
        return response.status_code


def test_a_server_with_an_untrusted_certificate_is_refused(
    monkeypatch: pytest.MonkeyPatch, tls_server: tuple[int, Path]
) -> None:
    port, _cert = tls_server
    monkeypatch.setattr(tls, "_client_ssl_context", None)
    for variable in ("HTTPS_PROXY", "https_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(variable, raising=False)

    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        _fetch_from(port)


def test_a_certificate_authority_in_ssl_cert_file_is_trusted(
    monkeypatch: pytest.MonkeyPatch, tls_server: tuple[int, Path]
) -> None:
    port, cert_path = tls_server
    monkeypatch.setattr(tls, "_client_ssl_context", None)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))

    assert _fetch_from(port) == 200


def test_the_fetch_always_verifies_the_certificate_and_its_host() -> None:
    context = github_images.client_ssl_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_a_fetch_logs_no_url(fake: FakeGitHub, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    _read(ASSET, Transport(_redirect(SIGNED), _image()))

    assert "user-attachments" not in caplog.text
    assert "githubusercontent" not in caplog.text
