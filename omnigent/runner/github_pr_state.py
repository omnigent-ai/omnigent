"""A GitHub PR's state, draft flag and title in one conditional read.

The server's listed-session refresher stores only these three fields, so it asks
for a summary instead of the panel's full read (``gh auth status``, ``gh pr view``
with checks, reviews and comments, then the conversation GraphQL query). The
summary is one ``gh api -i repos/{repo}/pulls/{n}``, sent with ``If-Modified-Since``
once a PR has been read; GitHub answers an unchanged PR with 304, which does not
count against the rate limit. The PR's ``Last-Modified`` is its ``updated_at``,
which GitHub moves on every state, draft and title change, so a 304 never hides
one of them. The ETag cannot serve: GitHub's differs on every read.
"""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from datetime import datetime
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any

from omnigent.runner import github_resource
from omnigent.runner.session_prs import PullRequestRef

_logger = logging.getLogger(__name__)

# PRs whose last read is kept for a conditional request; each entry is a few strings.
_CACHE_LIMIT = 256
# url -> (Last-Modified header, the PR fields that response carried)
_CACHE: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()
_CACHE_LOCK = Lock()

_STATUS_LINE = re.compile(r"HTTP/\S+\s+(\d{3})")
_HEADER_END = re.compile(r"\r?\n\r?\n")


def _http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _newer(left: str, right: str) -> bool:
    """Whether HTTP date ``left`` is strictly later than ``right``."""
    left_at, right_at = _http_date(left), _http_date(right)
    return left_at is not None and right_at is not None and left_at > right_at


def _parse_response(out: str) -> tuple[int | None, dict[str, str], str]:
    """Split ``gh api -i`` output into its status, lower-cased headers and body."""
    head, *rest = _HEADER_END.split(out, maxsplit=1)
    body = rest[0] if rest else ""
    lines = head.splitlines()
    match = _STATUS_LINE.match(lines[0]) if lines else None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return (int(match.group(1)) if match else None), headers, body


def _pr_fields(reference: PullRequestRef, data: object) -> dict[str, Any] | None:
    """The ``pr`` fields the server stores, from a REST pull request."""
    if not isinstance(data, dict):
        return None
    state = data.get("state")
    if data.get("merged") is True or data.get("merged_at"):
        state = "MERGED"
    elif state == "open":
        state = "OPEN"
    elif state == "closed":
        state = "CLOSED"
    else:
        return None
    title = data.get("title")
    return {
        "number": reference.number,
        "url": reference.url,
        "title": title if isinstance(title, str) else None,
        "state": state,
        "is_draft": data.get("draft") is True,
    }


def reference_state(root: str, reference: PullRequestRef) -> dict[str, Any] | None:
    """Read one GitHub PR's state, draft flag and title, or ``None`` when GitHub can't say.

    :param root: Absolute workspace path, the ``gh`` working directory.
    :param reference: A GitHub PR the session tracks.
    """
    try:
        host_args = github_resource._host_args(root, reference)
    except ValueError:  # Not signed in to this GitHub Enterprise host.
        return None
    with _CACHE_LOCK:
        cached = _CACHE.get(reference.url)
    argv = ["api", "-i", *host_args]
    if cached is not None:
        argv += ["-H", f"If-Modified-Since: {cached[0]}"]
    # Without skip_mergeability_checks GitHub recomputes the merge commit on each read,
    # and this read needs only state, draft and title.
    argv.append(
        f"repos/{reference.repository}/pulls/{reference.number}?skip_mergeability_checks=true"
    )
    rc, out, _ = github_resource._gh(
        argv, cwd=root, token=github_resource._pr_token(root, reference)
    )
    status, headers, body = _parse_response(out)
    if status == 304 and cached is not None:
        with _CACHE_LOCK:
            current = _CACHE.get(reference.url)
            if current is not None:
                _CACHE.move_to_end(reference.url)
                # A concurrent read may have stored a newer response meanwhile.
                if _newer(current[0], cached[0]):
                    cached = current
        return dict(cached[1])
    fields = None
    if rc == 0 and status == 200:
        try:
            fields = _pr_fields(reference, json.loads(body))
        except ValueError:
            _logger.debug("GitHub returned an unreadable pull request", exc_info=True)
    last_modified = headers.get("last-modified")
    modified, served = _http_date(last_modified), _http_date(headers.get("date"))
    with _CACHE_LOCK:
        current = _CACHE.get(reference.url)
        # HTTP dates have one-second resolution: a change later in the second the
        # PR last changed would compare as not modified, so such a read is not reused.
        if fields is not None and last_modified and modified and served and served > modified:
            # Concurrent reads can finish out of order; the cache only moves forward.
            if current is not None and _newer(current[0], last_modified):
                return dict(current[1])
            _CACHE[reference.url] = (last_modified, fields)
            _CACHE.move_to_end(reference.url)
            while len(_CACHE) > _CACHE_LIMIT:
                _CACHE.popitem(last=False)
        elif current is not None and current is cached:
            # An error drops the validator it was sent with, so the next read is
            # unconditional; an entry a concurrent read stored since stays.
            del _CACHE[reference.url]
    return None if fields is None else dict(fields)
