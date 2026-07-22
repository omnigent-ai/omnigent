"""Per-journey network-request capture and aggregation.

A :class:`NetCapture` attaches to a Playwright page for exactly one timed
journey repetition, tallies every request the browser issues, and produces two
groupings:

- **by resource type** — ``document`` / ``script`` / ``stylesheet`` / ``xhr`` /
  ``fetch`` / ``websocket`` / ``image`` / ``font`` / … (Playwright's
  ``request.resource_type``).
- **by endpoint** — ``"{METHOD} {normalized_path}"`` where volatile path
  segments (session ids, agent ids, hashes) are collapsed to placeholders so
  ``GET /v1/sessions/:id`` aggregates across repetitions instead of fanning out
  into one bucket per id.

:func:`aggregate_network` folds the per-rep tallies of a journey into a single
report block. Counts are reported as the **median across reps** (not the sum),
so the numbers are independent of how many iterations were run and a stray
one-off request doesn't inflate the headline. ``per_rep_total_requests`` and
``max_total_requests`` are kept alongside so nondeterminism stays visible.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from playwright.async_api import Page, Request, WebSocket

# Path-segment normalizers, applied in order to each ``/``-split segment so
# volatile ids collapse to stable placeholders and endpoint counts aggregate.
_ID_NORMALIZERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 32-hex session/conversation ids and uuids (with or without dashes).
    (re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE), ":id"),
    (
        re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            re.IGNORECASE,
        ),
        ":id",
    ),
    # Prefixed ids the API mints, e.g. ``conv_ab12``, ``ag_cd34``, ``msg_...``.
    (re.compile(r"^(?:conv|ag|msg|resp|run|ci|host|item)_[0-9a-z]+$", re.IGNORECASE), ":id"),
    # Bare numeric segments.
    (re.compile(r"^\d+$"), ":n"),
)

# Vite/rolldown build-hashed asset chunks: one or more ``-<fingerprint>`` groups
# before the extension, e.g. ``index-a1b2c3d4.js`` or the double-hashed
# ``chunk-CSCIHK7Q-Cqn_ZLae.js``. A fingerprint is 8+ base64url-ish chars
# containing at least one uppercase letter, digit, or underscore — so a plain
# lowercase word suffix (``jsx-runtime.js``, ``preload-helper.js``) is left
# alone. Stripping every deploy's hashes lets asset counts aggregate across
# builds (the hashes change each build, but the stem is stable).
_ASSET_RE = re.compile(
    r"^(?P<stem>.+?)(?:-(?=[A-Za-z0-9_]*[A-Z0-9_])[A-Za-z0-9_]{8,})+"
    r"\.(?P<ext>js|css|woff2?|map)$"
)


def normalize_path(url: str) -> str:
    """Reduce *url* to a ``/``-path with volatile segments placeholdered.

    The query string and fragment are dropped (they carry per-request nonces
    and cursor tokens that would otherwise explode the endpoint cardinality).

    :param url: The absolute request URL.
    :returns: The normalized path, e.g. ``/v1/sessions/:id/items``.
    """
    path = urlsplit(url).path or "/"
    out: list[str] = []
    for segment in path.split("/"):
        if not segment:
            continue
        out.append(_normalize_segment(segment))
    return "/" + "/".join(out) if out else "/"


def _normalize_segment(segment: str) -> str:
    """Collapse one path segment to a placeholder if it matches a volatile shape."""
    for pattern, replacement in _ID_NORMALIZERS:
        if pattern.match(segment):
            return replacement
    # Hashed-asset rule: keep the human stem, drop the build fingerprint(s).
    asset = _ASSET_RE.match(segment)
    if asset is not None:
        return f"{asset.group('stem')}.{asset.group('ext')}"
    return segment


@dataclass
class RepCapture:
    """One repetition's request tally, collected live off the page.

    :param by_resource_type: Count of requests per Playwright resource type.
    :param by_endpoint: Count of requests per ``"{METHOD} {normalized_path}"``.
    :param websocket_opened: Number of WebSocket connections opened.
    :param total: Total requests observed in the timed window.
    """

    by_resource_type: Counter[str] = field(default_factory=Counter)
    by_endpoint: Counter[str] = field(default_factory=Counter)
    websocket_opened: int = 0
    total: int = 0


class NetCapture:
    """Attaches request/websocket listeners to a page for one timed rep.

    Use as a context-managed span around exactly the timed operation::

        cap = NetCapture(page)
        cap.start()
        ...  # the timed journey action
        rep = cap.stop()

    ``start``/``stop`` bracket the listeners so requests fired by setup or
    between reps are never counted. A single :class:`NetCapture` instance is
    reusable — each ``start`` resets the tally.
    """

    def __init__(self, page: Page) -> None:
        self._page = page
        self._rep = RepCapture()
        self._active = False

    def _on_request(self, request: Request) -> None:
        if not self._active:
            return
        self._rep.total += 1
        self._rep.by_resource_type[request.resource_type or "other"] += 1
        self._rep.by_endpoint[f"{request.method} {normalize_path(request.url)}"] += 1

    def _on_websocket(self, ws: WebSocket) -> None:
        if not self._active:
            return
        self._rep.websocket_opened += 1
        self._rep.by_resource_type["websocket"] += 1
        self._rep.by_endpoint[f"WS {normalize_path(ws.url)}"] += 1

    def start(self) -> None:
        """Begin counting: reset the tally and arm the listeners."""
        self._rep = RepCapture()
        if not self._active:
            self._page.on("request", self._on_request)
            self._page.on("websocket", self._on_websocket)
            self._active = True

    def stop(self) -> RepCapture:
        """Stop counting, detach the listeners, and return this rep's tally."""
        if self._active:
            self._page.remove_listener("request", self._on_request)
            self._page.remove_listener("websocket", self._on_websocket)
            self._active = False
        return self._rep


def _median_counter(counters: list[Counter[str]]) -> dict[str, float]:
    """Median count per key across *counters* (absent key counts as 0).

    Every key seen in any rep is scored across ALL reps — a key present in only
    some reps contributes 0 for the reps that lack it, so its median reflects
    how consistently it fires, not just its value when it does.
    """
    if not counters:
        return {}
    keys = set().union(*counters)
    out: dict[str, float] = {}
    for key in keys:
        out[key] = statistics.median(c.get(key, 0) for c in counters)
    # Stable, high-count-first ordering for a readable report.
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def aggregate_network(reps: list[RepCapture]) -> dict[str, object]:
    """Fold a journey's per-rep captures into a report ``network`` block.

    :param reps: One :class:`RepCapture` per timed repetition (warmup excluded).
    :returns: A JSON-serializable dict with ``by_resource_type`` /
        ``by_endpoint`` median-per-rep count maps, plus totals and the raw
        per-rep totals so nondeterminism is visible. Empty maps when *reps* is
        empty.
    """
    if not reps:
        return {
            "aggregation": "median_per_rep",
            "reps": 0,
            "by_resource_type": {},
            "by_endpoint": {},
            "median_total_requests": 0.0,
            "max_total_requests": 0,
            "websocket_opened_median": 0.0,
            "per_rep_total_requests": [],
        }
    totals = [r.total for r in reps]
    return {
        "aggregation": "median_per_rep",
        "reps": len(reps),
        "by_resource_type": _median_counter([r.by_resource_type for r in reps]),
        "by_endpoint": _median_counter([r.by_endpoint for r in reps]),
        "median_total_requests": statistics.median(totals),
        "max_total_requests": max(totals),
        "websocket_opened_median": statistics.median(r.websocket_opened for r in reps),
        "per_rep_total_requests": totals,
    }
