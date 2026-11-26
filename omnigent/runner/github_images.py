"""Images a pull request links to, fetched as the pull request's GitHub account.

An image is served only when the pull request's description or comments link to its
exact URL, the URL is on one of GitHub's image hosts for that pull request's host, and
the bytes are a raster image. The token goes on the first request only, and only to the
pull request's own host. Signed redirect URLs carry a credential of their own, so no URL
is ever logged, cached or put in an error.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import ssl
import threading
import time
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from omnigent.runner import github_failures
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
# Revisions kept in the linked-URL cache; the oldest is dropped first.
MAX_CACHED_REVISIONS = 64
# An edit that doesn't bump the pull request's update time is seen within this.
LINKED_URLS_TTL_SECONDS = 300.0
# Bounds the whole fetch, every hop and the body, so no read holds a slot past it.
FETCH_DEADLINE_SECONDS = 30.0
# How long a read waits for a slot, inside the server's wait for the first reply.
SLOT_WAIT_SECONDS = 10.0
# Each hop's connect, header and body reads, cut to the fetch's time left.
_HOP_TIMEOUT_SECONDS = 10.0
# The pull request read and its listings share the gh deadline other PR actions use.
_READ_PULL_REQUEST_SECONDS = 15.0
_PAGE_SIZE = 100
# A listing stops after this many pages, so a very long thread links only its first items.
_MAX_LISTING_PAGES = 20
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_URL = re.compile(r"https://[^\s<>\"'()\]\[]+")
# The asset path an upload answers with, as ``github_resource._is_attachment_url`` matches it.
_ATTACHMENT_PATH = re.compile(r"/user-attachments/assets/[0-9a-f-]+")
_GITHUB_IMAGE_HOSTS = frozenset(
    {"private-user-images.githubusercontent.com", "user-images.githubusercontent.com"}
)
# Encoded dots and slashes in the path could reach one the allowlist never checked. A query
# may carry them: GitHub's signed image URLs do.
_ENCODED_TRAVERSAL = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_UNKNOWN_HOST = "Sign in to this GitHub host with gh, or set GH_HOST to it, before viewing its PRs"
_READ_FAILED = "GitHub returned an unexpected pull request response."
_AUTHENTICATION_TIMED_OUT = "GitHub authentication timed out. Try again."
_IMAGE_MESSAGES = {
    "not_in_pr": "That image isn't linked from this pull request.",
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
    """The bytes of an image a pull request links to, and the type they were checked as."""

    content_type: str
    data: bytes


def checked_image_url(url: str, pr_host: str) -> httpx.URL:
    """``url`` parsed once, when it is on an allowed image host for a PR on ``pr_host``.

    The parsed value is the one that is fetched, so no other parser can see another host
    or path. A URL that parsing would change, such as one with dot segments, is refused.

    :raises ImageRefused: ``host_not_allowed``.
    """
    without_fragment = url.split("#", 1)[0]
    before_query = without_fragment.split("?", 1)[0]
    if (
        any(ord(character) <= 0x20 or ord(character) == 0x7F for character in without_fragment)
        or "\\" in without_fragment
        or _ENCODED_TRAVERSAL.search(before_query)
    ):
        raise ImageRefused("host_not_allowed")
    try:
        parsed = httpx.URL(without_fragment)
    except httpx.InvalidURL:
        raise ImageRefused("host_not_allowed") from None
    if str(parsed) != without_fragment or not _allowed(parsed, pr_host.lower()):
        raise ImageRefused("host_not_allowed")
    return parsed


def _allowed(url: httpx.URL, pr_host: str) -> bool:
    if url.scheme != "https" or url.userinfo or url.port not in (None, 443):
        return False
    host = url.raw_host.decode("ascii", "replace")
    if not _plain_hostname(host):
        return False
    path = url.raw_path.split(b"?", 1)[0].decode("ascii", "replace")
    if any(segment in (".", "..") for segment in path.split("/")):
        return False
    if pr_host == "github.com":
        if host == "github.com":
            return _ATTACHMENT_PATH.fullmatch(path) is not None
        return host in _GITHUB_IMAGE_HOSTS
    if host == pr_host:
        return _ATTACHMENT_PATH.fullmatch(path) is not None or path.startswith("/storage/user/")
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


_linked_urls_lock = threading.Lock()
# Keyed by host, repository, number, head SHA and update time; one revision per pull request.
_linked_urls_cache: OrderedDict[tuple[str, str, int, str, str], tuple[frozenset[str], float]] = (
    OrderedDict()
)
_read_slots = threading.BoundedSemaphore(MAX_CONCURRENT_READS)


def _gh_json(root: str, reference: PullRequestRef, endpoint: str) -> object:
    rc, output, err = github._gh(
        ["api", *github._host_args(root, reference), endpoint],
        cwd=root,
        token=github._pr_token(root, reference),
    )
    if rc != 0:
        raise github_failures.refusal(err, reference=reference, action="read this pull request")
    try:
        return json.loads(output)
    except ValueError as exc:
        raise ValueError(_READ_FAILED) from exc


def _pull_request(root: str, reference: PullRequestRef) -> dict[str, Any]:
    payload = _gh_json(root, reference, f"repos/{reference.repository}/pulls/{reference.number}")
    if not isinstance(payload, dict):
        raise ValueError(_READ_FAILED)
    return payload


def _listing(root: str, reference: PullRequestRef, collection: str) -> list[dict[str, Any]]:
    """Every item of a pull request's list, page by page, up to a fixed page bound."""
    items: list[dict[str, Any]] = []
    for page in range(1, _MAX_LISTING_PAGES + 1):
        payload = _gh_json(root, reference, f"{collection}?per_page={_PAGE_SIZE}&page={page}")
        if not isinstance(payload, list):
            raise ValueError(_READ_FAILED)
        items.extend(item for item in payload if isinstance(item, dict))
        if len(payload) < _PAGE_SIZE:
            break
    return items


def _linked_urls(
    root: str, reference: PullRequestRef, pull_request: dict[str, Any]
) -> frozenset[str]:
    """Every https URL in the pull request's description, comments, reviews and review comments."""
    repo, number = reference.repository, reference.number
    bodies = [pull_request.get("body")]
    for collection in (
        f"repos/{repo}/issues/{number}/comments",
        f"repos/{repo}/pulls/{number}/comments",
        f"repos/{repo}/pulls/{number}/reviews",
    ):
        bodies.extend(item.get("body") for item in _listing(root, reference, collection))
    urls: set[str] = set()
    for text in bodies:
        if isinstance(text, str):
            for match in _URL.finditer(html.unescape(text)):
                urls.add(match.group(0).rstrip(".,;:!?"))
    return frozenset(urls)


def _revision(pull_request: dict[str, Any]) -> tuple[str, str] | None:
    """The head SHA and last update that identify the pull request's current revision."""
    head = pull_request.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    updated_at = pull_request.get("updated_at")
    if isinstance(head_sha, str) and isinstance(updated_at, str):
        return head_sha, updated_at
    return None


def linked_image_urls(
    root: str, reference: PullRequestRef, *, clock: Callable[[], float] = time.monotonic
) -> frozenset[str]:
    """Every https URL the pull request links to, cached per revision for a short while.

    The pull request is read first on every call, as the call's account, so the cache
    never answers for an account or session that can't read it.
    """
    pull_request = _pull_request(root, reference)
    revision = _revision(pull_request)
    if revision is None:
        return _linked_urls(root, reference, pull_request)
    pull_request_key = (reference.host, reference.repository.casefold(), reference.number)
    key = (*pull_request_key, *revision)
    with _linked_urls_lock:
        cached = _linked_urls_cache.get(key)
        if cached is not None and clock() - cached[1] < LINKED_URLS_TTL_SECONDS:
            _linked_urls_cache.move_to_end(key)
            return cached[0]
    urls = _linked_urls(root, reference, pull_request)
    with _linked_urls_lock:
        for stale in [known for known in _linked_urls_cache if known[:3] == pull_request_key]:
            del _linked_urls_cache[stale]
        _linked_urls_cache[key] = (urls, clock())
        while len(_linked_urls_cache) > MAX_CACHED_REVISIONS:
            _linked_urls_cache.popitem(last=False)
    return urls


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
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "image/avif"
    return None


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
            async with transport or _transport_for(current) as hop_transport:
                response = await hop_transport.handle_async_request(request)
                try:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location or hop == MAX_REDIRECTS:
                            raise ImageRefused("fetch_failed")
                        current = checked_image_url(str(current.join(location)), pr_host)
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
    """The image at ``image_url``, when the pull request links to it. Blocking.

    The pull request read, its listings and the fetch all run as one account. A host
    the user hasn't set up for GitHub is refused before ``gh`` runs or a token is read.

    :raises ImageRefused: With a reason code: ``not_in_pr``, ``host_not_allowed``,
        ``too_large``, ``not_image`` or ``fetch_failed``. The message never names the URL.
    :raises ValueError: When the pull request can't be read.
    """
    url = checked_image_url(image_url, reference.host)
    if not github._known_github_host(reference.host):
        raise ValueError(_UNKNOWN_HOST)
    with github._one_pr_account(root, reference, timed_out=_AUTHENTICATION_TIMED_OUT):
        with github._gh_deadline_scope(time.monotonic() + _READ_PULL_REQUEST_SECONDS):
            linked = linked_image_urls(root, reference, clock=clock)
        if image_url not in linked:
            raise ImageRefused("not_in_pr")
        token = github._pr_token(root, reference)
    if not _read_slots.acquire(timeout=SLOT_WAIT_SECONDS):
        raise ImageRefused("fetch_failed")
    try:
        return _fetch_in_time(
            url, pr_host=reference.host, token=token, transport=transport, clock=clock
        )
    finally:
        _read_slots.release()
