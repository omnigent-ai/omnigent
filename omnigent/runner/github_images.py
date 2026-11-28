"""Attachment images on a pull request's host, fetched as the pull request's GitHub account.

An image is served only when its URL is one of the pull request host's own attachments,
``https://<host>/user-attachments/assets/<id>``, its redirects stay on that host's signed
image host, and the bytes are a raster image. The token goes on the first request only, to
the pull request's own host. Signed redirect URLs carry a credential of their own, so no
URL is ever logged, cached or put in an error.
"""

from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import logging
import re
import ssl
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import httpx

from omnigent.runner import github_resource as github
from omnigent.runner.pull_request_errors import PullRequestError
from omnigent.runner.session_prs import PullRequestRef
from omnigent.util.tls import client_ssl_context

# Raster types only: an SVG or HTML document can carry script.
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"})
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3
# Image reads this host process runs at once; the others wait for a slot.
MAX_CONCURRENT_READS = 4
# Bounds the whole fetch, every hop and the body. A stalled DNS lookup can outlast it: the
# loop's shutdown waits for the resolver thread, so the read keeps its slot until that times out.
FETCH_DEADLINE_SECONDS = 30.0
# How long a read waits for a slot, inside the server's wait for the first reply.
SLOT_WAIT_SECONDS = 10.0
# Each hop's connect, header and body reads, cut to the fetch's time left.
_HOP_TIMEOUT_SECONDS = 10.0
# Reading the account's token shares the gh deadline other PR actions use.
_READ_TOKEN_SECONDS = 15.0
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Where github.com's attachment URLs redirect: signed, short-lived image URLs.
_GITHUB_SIGNED_IMAGE_HOST = "private-user-images.githubusercontent.com"
# Encoded dots and slashes in the path could reach one the allowlist never checked. A query
# may carry them: GitHub's signed image URLs do.
_ENCODED_TRAVERSAL = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_UNKNOWN_HOST = "Sign in to this GitHub host with gh, or set GH_HOST to it, before viewing its PRs"
_AUTHENTICATION_TIMED_OUT = "GitHub authentication timed out. Try again."
_IMAGE_MESSAGES = {
    "host_not_allowed": "GitHub won't show images from that address.",
    "too_large": "That image is larger than 10 MB.",
    "not_image": "That file isn't a supported image.",
    "fetch_failed": "GitHub couldn't load that image. Try again.",
}
IMAGE_REFUSAL_REASONS = frozenset(_IMAGE_MESSAGES)


class ImageRefused(PullRequestError):
    """An image that can't be shown; the reason code and a fixed message are all it says."""

    def __init__(self, reason: str) -> None:
        super().__init__(_IMAGE_MESSAGES[reason])
        self.reason = reason


@dataclass(frozen=True)
class PullRequestImage:
    """The bytes of an attachment image, and the type they were checked as."""

    content_type: str
    data: bytes


def checked_image_url(url: str, pr_host: str) -> httpx.URL:
    """``url`` parsed once, when it is an attachment on ``pr_host``, the PR's own host.

    Only ``https://<pr_host>/user-attachments/assets/<id>`` is accepted, with no query; a
    fragment is dropped. The parsed value is the one that is fetched, so no other parser
    can see another host or path.

    :raises ImageRefused: ``host_not_allowed``.
    """
    host = pr_host.lower()
    without_fragment = url.split("#", 1)[0]
    if not github._is_attachment_url(without_fragment, host):
        raise ImageRefused("host_not_allowed")
    parsed = _parsed(without_fragment)
    if parsed is None or not _plain_hostname(host):
        raise ImageRefused("host_not_allowed")
    return parsed


def _parsed(reference: str) -> httpx.URL | None:
    """``reference`` parsed, when it is an https URL parsing leaves exactly as written."""
    if _raw_reference_refused(reference):
        return None
    try:
        parsed = httpx.URL(reference)
    except httpx.InvalidURL:
        return None
    if str(parsed) != reference or parsed.scheme != "https" or parsed.userinfo:
        return None
    if parsed.port not in (None, 443):
        return None
    return parsed


def _raw_reference_refused(reference: str) -> bool:
    """Whether a URL or redirect reference, as written, breaks a rule that resolving it hides.

    Control characters, spaces, backslashes, dot segments and encoded dots or slashes in the
    path are refused before parsing, since resolving a reference rewrites each of them.
    """
    before_query = reference.split("?", 1)[0]
    return (
        any(ord(character) <= 0x20 or ord(character) == 0x7F for character in reference)
        or "\\" in reference
        or _ENCODED_TRAVERSAL.search(before_query) is not None
        or any(segment in (".", "..") for segment in before_query.split("/"))
    )


def _redirect_target(current: httpx.URL, location: str, pr_host: str) -> httpx.URL:
    """Where a redirect goes, checked as written and once resolved: the signed image host.

    :raises ImageRefused: ``host_not_allowed``.
    """
    reference = location.split("#", 1)[0]
    if _raw_reference_refused(reference):
        raise ImageRefused("host_not_allowed")
    # A location that names its host is checked as written, case and port included; a
    # scheme-relative one takes the current scheme, which is always https.
    if reference.lower().startswith("https:"):
        target = _parsed(reference)
    elif reference.startswith("//"):
        target = _parsed(f"https:{reference}")
    else:
        target = _parsed(str(current.join(reference)))
    if target is None or not _signed_image_url(target, pr_host.lower()):
        raise ImageRefused("host_not_allowed")
    return target


def _signed_image_url(url: httpx.URL, pr_host: str) -> bool:
    """Whether ``url`` is on the signed image host ``pr_host``'s attachments redirect to.

    That is ``private-user-images.githubusercontent.com`` for github.com. An Enterprise
    Server serves them from ``media.<host>``, or under ``/storage/user/`` on itself.
    """
    host = url.raw_host.decode("ascii", "replace")
    if not _plain_hostname(host):
        return False
    if pr_host == "github.com":
        return host == _GITHUB_SIGNED_IMAGE_HOST
    if host == pr_host:
        return url.raw_path.split(b"?", 1)[0].startswith(b"/storage/user/")
    return host == f"media.{pr_host}"


def _plain_hostname(host: str) -> bool:
    """A lowercase DNS name: no IP literal, no trailing dot, and no punycode label."""
    if not host or host != host.lower() or host.endswith(".") or host.startswith("["):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    return not any(label.startswith("xn--") for label in host.split("."))


_read_slots = threading.BoundedSemaphore(MAX_CONCURRENT_READS)


def _sniff(data: bytes) -> str | None:
    """The image type the leading bytes say, or ``None``."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if _avif_brand(data):
        return "image/avif"
    return None


# An ISO base media file starts with an ftyp box: size, "ftyp", major brand, minor version,
# then the compatible brands. A real one names a few; the cap bounds the scan.
_FTYP_HEADER_BYTES = 16
_MAX_FTYP_BYTES = 256
_AVIF_BRANDS = frozenset({b"avif", b"avis"})


def _avif_brand(data: bytes) -> bool:
    """Whether a well-formed leading ftyp box names AVIF as its major or a compatible brand."""
    if data[4:8] != b"ftyp":
        return False
    size = int.from_bytes(data[:4], "big")
    if not _FTYP_HEADER_BYTES <= size <= min(_MAX_FTYP_BYTES, len(data)) or size % 4:
        return False
    brands = [data[8:12]] + [data[offset : offset + 4] for offset in range(16, size, 4)]
    return any(brand in _AVIF_BRANDS for brand in brands)


async def _read_image(
    response: httpx.Response, *, deadline: float, clock: Callable[[], float]
) -> PullRequestImage:
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in IMAGE_TYPES:
        raise ImageRefused("not_image")
    # Refused before decoding, so a compressed answer is never expanded at all.
    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise ImageRefused("not_image")
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
        raise ImageRefused("too_large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_IMAGE_BYTES:
            raise ImageRefused("too_large")
        if clock() > deadline:
            raise ImageRefused("fetch_failed")
        chunks.append(chunk)
    data = b"".join(chunks)
    if _sniff(data) != content_type:
        raise ImageRefused("not_image")
    return PullRequestImage(content_type=content_type, data=data)


# Set while an image fetch runs, so its transport records can be told from other requests'.
_fetching_image = contextvars.ContextVar("fetching_image", default=False)


class _DropImageFetchRecords(logging.Filter):
    """Drops httpcore's records of an image fetch: at DEBUG they hold the redirect URLs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno > logging.DEBUG or not _fetching_image.get()


for _name in ("connection", "http11", "http2", "proxy", "socks"):
    logging.getLogger(f"httpcore.{_name}").addFilter(_DropImageFetchRecords())


@contextmanager
def _untraced() -> Iterator[None]:
    """No HTTP client spans or trace headers: a span would record each signed URL."""
    try:
        from opentelemetry.instrumentation.utils import suppress_http_instrumentation
    except ImportError:
        # Without the instrumentation library nothing traces these requests.
        yield
        return
    with suppress_http_instrumentation():
        yield


def _transport_for(url: httpx.URL) -> httpx.AsyncHTTPTransport:
    """A transport for one request, with TLS always verified and the proxy set explicitly.

    Requests go through a transport, not a client: a client logs every URL it fetches,
    keeps cookies across hops and reads ``.netrc``.
    """
    proxy = urllib.request.getproxies().get("https")
    if proxy and urllib.request.proxy_bypass(url.host):
        proxy = None
    return httpx.AsyncHTTPTransport(verify=client_ssl_context(), trust_env=False, proxy=proxy)


async def _fetch(
    url: httpx.URL,
    *,
    pr_host: str,
    token: str | None,
    transport: httpx.AsyncBaseTransport | None,
    clock: Callable[[], float],
) -> PullRequestImage:
    """Fetch an allowed image, following at most ``MAX_REDIRECTS`` checked redirects."""
    deadline = clock() + FETCH_DEADLINE_SECONDS
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        left = deadline - clock()
        if left <= 0:
            raise ImageRefused("fetch_failed")
        timeout = httpx.Timeout(min(_HOP_TIMEOUT_SECONDS, left)).as_dict()
        headers = {"Accept": "image/*", "Accept-Encoding": "identity"}
        # GitHub's attachment URL redirects to a signed URL that needs no token.
        if hop == 0 and token is not None and current.raw_host.decode() == pr_host.lower():
            headers["Authorization"] = f"token {token}"
        request = httpx.Request("GET", current, headers=headers, extensions={"timeout": timeout})
        try:
            hop_transport = transport or _transport_for(current)
        # An HTTPS_PROXY httpx can't use: an unknown scheme, a malformed URL, or SOCKS without
        # socksio. Its message names the proxy, so none is passed on.
        except (ValueError, httpx.InvalidURL, ImportError):
            raise ImageRefused("fetch_failed") from None
        try:
            async with hop_transport:
                response = await hop_transport.handle_async_request(request)
                try:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location or hop == MAX_REDIRECTS:
                            raise ImageRefused("fetch_failed")
                        current = _redirect_target(current, location, pr_host)
                        continue
                    if response.status_code != 200:
                        raise ImageRefused("fetch_failed")
                    return await _read_image(response, deadline=deadline, clock=clock)
                finally:
                    await response.aclose()
        # Their messages can name the URL, and a signed URL is a credential.
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, ssl.SSLError, OSError):
            raise ImageRefused("fetch_failed") from None
    raise ImageRefused("fetch_failed")


def _fetch_in_time(
    url: httpx.URL,
    *,
    pr_host: str,
    token: str | None,
    transport: httpx.AsyncBaseTransport | None,
    clock: Callable[[], float],
) -> PullRequestImage:
    """The fetch, every hop and the body, cut off at one wall-clock deadline.

    Each hop's own waits are capped too, but only this bounds their sum. Blocking: it runs
    its own event loop, so call it off any running one.
    """

    async def within_deadline() -> PullRequestImage:
        _fetching_image.set(True)
        with _untraced():
            async with asyncio.timeout(FETCH_DEADLINE_SECONDS):
                return await _fetch(
                    url, pr_host=pr_host, token=token, transport=transport, clock=clock
                )

    try:
        return asyncio.run(within_deadline())
    except TimeoutError:
        raise ImageRefused("fetch_failed") from None


def read_pull_request_image(
    root: str,
    reference: PullRequestRef,
    image_url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> PullRequestImage:
    """The attachment image at ``image_url`` on the pull request's host. Blocking.

    The fetch runs as the pull request's account. A host the user hasn't set up for
    GitHub is refused before ``gh`` runs or a token is read.

    :raises ImageRefused: With a reason code: ``host_not_allowed``, ``too_large``,
        ``not_image`` or ``fetch_failed``. The message never names the URL.
    :raises ValueError: When the account's token can't be read.
    """
    url = checked_image_url(image_url, reference.host)
    if not github._known_github_host(reference.host):
        raise ValueError(_UNKNOWN_HOST)
    with (
        github._one_pr_account(root, reference, timed_out=_AUTHENTICATION_TIMED_OUT),
        github._gh_deadline_scope(time.monotonic() + _READ_TOKEN_SECONDS),
    ):
        token = _image_token(root, reference)
    if not _read_slots.acquire(timeout=SLOT_WAIT_SECONDS):
        raise ImageRefused("fetch_failed")
    try:
        return _fetch_in_time(
            url, pr_host=reference.host, token=token, transport=transport, clock=clock
        )
    finally:
        _read_slots.release()


def _image_token(root: str, reference: PullRequestRef) -> str:
    """The bound account's token; in a managed sandbox, the owner's brokered one.

    A sandbox binds no token, because ``gh`` reads the owner's from its ``hosts.yml``. The
    fetch doesn't go through ``gh``, so it asks ``gh`` for that token, which ignores the
    ambient token variables there. Without a token the fetch would run signed out, so it
    is refused instead.

    :raises ValueError: When the token can't be read, or reading it timed out.
    """
    token = github._pr_token(root, reference)
    if not token and github._in_sandbox():
        token = github._gh_active_token(root, reference.host)
        if github._gh_timed_out.get():
            raise ValueError(_AUTHENTICATION_TIMED_OUT)
    if not token:
        raise ValueError(github._ACTIVE_ACCOUNT_UNREADABLE)
    return token
