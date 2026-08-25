"""A deterministic in-test ``routes:select`` service.

Replaces the Databricks AI-Gateway ``task_v1`` router for the e2e routing CUJs.
It speaks the exact wire contract :class:`~omnigent.server.smart_routing.ExternalRoutingClient`
speaks — the ``omnigent.api.routing.v1`` protos, snake_case JSON, one
``route_selection`` entry plus a ``rationale`` — and reproduces the observed
task_v1 behaviour the CUJ matrix in this directory is scored against:

* **Scenario inference from the arms present**, not the harness tags: Claude
  arms only → ``cc``, Codex arms only → ``codex``, both → ``both``.
* **Each scenario requires its full menu.** A narrowed menu is a 400 carrying
  the real router's message (``scenario 'codex' requires its full menu;
  missing [...]``), so a client that stops injecting arms fails here the way it
  fails against the gateway. Extra non-arm models are tolerated and ignored.
* **The harness tag is passthrough** — echoed verbatim on the selection,
  never read.
* Picks come only from ``route_options``, and the rationale is the same rule
  trace the live router emits (captured verbatim from staging logs), so the
  chips and decision cards under test carry production-shaped text.
* :attr:`MockRouter.disabled` reproduces a workspace whose account never had
  the routing API turned on: every ``routes:select`` answers the 404 that
  deployment gets (:data:`NOT_ENABLED_MESSAGE`), which is a permanent
  condition rather than an outage.

The rule table is :func:`decide`; it is the whole routing policy and is unit
tested by ``test_mock_router.py``.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# ── The frozen task_v1 arms (mirrors smart_routing._TASK_V1_*_ARMS) ─────────

CLAUDE_ARMS: tuple[str, ...] = ("claude-opus-4-8", "claude-sonnet-5")
CODEX_ARMS: tuple[str, ...] = ("glm-5-2", "gpt-5-6-sol", "gpt-5-6-luna")

MENUS: dict[str, tuple[str, ...]] = {
    "cc": CLAUDE_ARMS,
    "codex": CODEX_ARMS,
    "both": CLAUDE_ARMS + CODEX_ARMS,
}

#: Rule-0's cheapest arm per scenario. The ``cc`` menu has no luna, so the
#: claude-only scenario's cheapest arm is the sonnet pin.
CHEAPEST_ARM: dict[str, str] = {
    "cc": "claude-sonnet-5",
    "codex": "gpt-5-6-luna",
    "both": "gpt-5-6-luna",
}

#: Rule-0's trivial cutoff, in characters (task_v1's own boundary).
TRIVIAL_MAX_CHARS = 300

#: Upper bound of task_v1's ``prompt_short`` bucket, in characters.
PROMPT_SHORT_MAX_CHARS = 1200

#: The path the client appends to ``routing.base_url``.
ROUTES_SELECT_PATH = "/routes:select"

#: Base path the mock serves under, so the configured ``base_url`` looks like
#: the gateway's (``.../ai-gateway/routing/v1``) rather than a bare host.
BASE_PATH = "/ai-gateway/routing/v1"

#: What a workspace without the routing API answers, verbatim. The condition is
#: account-level, so it never clears on retry.
NOT_ENABLED_MESSAGE = "routing/v1/routes:select is not enabled for this account."

# Deterministic stand-ins for task_v1's "errors or code refs" regex family:
# stack traces, file paths, backticked symbols, code fences, CLI flags.
_CODE_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"`[^`\n]+`"),
    re.compile(r"```"),
    re.compile(r"\bTraceback\b"),
    re.compile(r"\b\w+\.(py|ts|tsx|go|rs|java|rb|js)\b"),
    re.compile(r"(?<!\w)--[a-z][a-z0-9-]+"),
    re.compile(r"\b(def|class|import|function)\s+\w"),
    re.compile(r"\bError\b|\bException\b"),
)

# Prompts that span surfaces / mix concerns fail ``not_crosscutting``, which is
# what sends the long CUJ prompts to the escalate/default arms.
_CROSSCUTTING_MARKERS: tuple[str, ...] = (
    "across",
    "every surface",
    "open questions",
    "brainstorm",
    "end to end",
    "everywhere",
    "whole repo",
    "migration",
)


def has_code_markers(prompt: str) -> bool:
    """Whether *prompt* carries an error or code reference.

    :param prompt: The task prompt as sent in ``task.prompt``.
    :returns: ``True`` when any code/error marker matches.
    """
    return any(pattern.search(prompt) for pattern in _CODE_MARKERS)


def is_trivial(prompt: str) -> bool:
    """Whether rule-0 fires: short, no errors or code refs, easy.

    :param prompt: The task prompt.
    :returns: ``True`` when the trivial rule applies.
    """
    return len(prompt) < TRIVIAL_MAX_CHARS and not has_code_markers(prompt)


def is_crosscutting(prompt: str) -> bool:
    """Whether *prompt* spans surfaces, failing ``not_crosscutting``.

    :param prompt: The task prompt.
    :returns: ``True`` when the prompt reads as crosscutting.
    """
    lowered = prompt.lower()
    return len(prompt) > PROMPT_SHORT_MAX_CHARS or any(
        marker in lowered for marker in _CROSSCUTTING_MARKERS
    )


def is_delegate_class(prompt: str) -> bool:
    """Whether ``not_crosscutting AND prompt_short`` both hold.

    The delegate bucket: a narrow, well-specified, code-shaped task. On the
    ``codex`` menu this is what task_v1 hands down to ``glm-5-2``.

    :param prompt: The task prompt.
    :returns: ``True`` when the delegate conjunction holds.
    """
    return has_code_markers(prompt) and not is_crosscutting(prompt)


def scenario_for(models: Sequence[str]) -> str | None:
    """Infer the router scenario from the model families on offer.

    :param models: Bare model ids from ``route_options``.
    :returns: ``"cc"``, ``"codex"``, ``"both"``, or ``None`` when no
        recognized arm appears (the real router 400s with
        "could not infer a scenario").
    """
    offered = {m.lower() for m in models}
    claude = bool(offered & set(CLAUDE_ARMS))
    codex = bool(offered & set(CODEX_ARMS))
    if claude and codex:
        return "both"
    if claude:
        return "cc"
    if codex:
        return "codex"
    return None


def missing_arms(scenario: str, models: Sequence[str]) -> list[str]:
    """Arms *scenario* requires that ``route_options`` did not offer.

    :param scenario: An inferred scenario key.
    :param models: Bare model ids from ``route_options``.
    :returns: The missing arm ids, menu order preserved.
    """
    offered = {m.lower() for m in models}
    return [arm for arm in MENUS[scenario] if arm not in offered]


@dataclass(frozen=True)
class Verdict:
    """One routing decision: the arm and the rule trace behind it."""

    model: str
    rationale: str


def decide(prompt: str, scenario: str) -> Verdict:
    """Apply the rule table for *scenario* to *prompt*.

    The single source of truth for what this mock routes. Rationales are the
    live router's own rule traces, captured verbatim from staging.

    :param prompt: The task prompt.
    :param scenario: ``"cc"``, ``"codex"`` or ``"both"``.
    :returns: The chosen arm and its rationale.
    """
    if is_trivial(prompt):
        arm = CHEAPEST_ARM[scenario]
        return Verdict(
            model=arm,
            rationale=(
                f"Routed to {arm} because trivial task "
                f"(prompt<300, no errors/refs, llm easy) -> cheapest arm {arm}; "
                "never escalate."
            ),
        )
    if is_delegate_class(prompt):
        if scenario == "codex":
            return Verdict(
                model="glm-5-2",
                rationale=(
                    "Routed to glm-5-2 because [not_crosscutting AND prompt_short] "
                    "all hold -> delegate down to glm-5-2."
                ),
            )
        # cc and both escalate a contained, code-referencing task.
        return Verdict(
            model="claude-opus-4-8",
            rationale=(
                "Routed to claude-opus-4-8 because [not_crosscutting AND "
                "not_mixed_change AND low_ambiguity] all hold -> escalate up to "
                "claude-opus-4-8."
            ),
        )
    if scenario == "cc":
        return Verdict(
            model="claude-sonnet-5",
            rationale=(
                "Routed to claude-sonnet-5 because [not_crosscutting AND "
                "not_mixed_change AND low_ambiguity] not all hold -> default "
                "claude-sonnet-5."
            ),
        )
    return Verdict(
        model="gpt-5-6-sol",
        rationale=(
            "Routed to gpt-5-6-sol because [not_crosscutting AND prompt_short] "
            "not all hold -> default gpt-5-6-sol."
        ),
    )


class MenuRejected(Exception):
    """The offered ``route_options`` are not a scenario's full menu."""

    def __init__(self, message: str) -> None:
        """
        :param message: The router's own rejection text.
        """
        super().__init__(message)
        self.message = message


def select_route(body: dict[str, Any]) -> dict[str, Any]:
    """Answer one ``routes:select`` request.

    :param body: The decoded snake_case request body.
    :returns: The ``SelectRouteResponse`` body to serialize.
    :raises MenuRejected: When no scenario can be inferred, or the inferred
        scenario's menu is incomplete — the real router's two 400s.
    """
    options = body.get("route_options") or []
    models = [str(o.get("model") or "") for o in options if isinstance(o, dict)]
    prompt = str((body.get("task") or {}).get("prompt") or "")
    scenario = scenario_for(models)
    if scenario is None:
        raise MenuRejected("could not infer a scenario from the offered route_options")
    absent = missing_arms(scenario, models)
    if absent:
        raise MenuRejected(
            f"scenario {scenario!r} requires its full menu; missing [{', '.join(absent)}]"
        )
    verdict = decide(prompt, scenario)
    # The tag is passthrough: echo whatever the caller attached to the picked
    # option, exactly as the gateway does, so a client that trusted it breaks
    # here too.
    harness = next(
        (
            o.get("harness")
            for o in options
            if isinstance(o, dict) and str(o.get("model") or "").lower() == verdict.model
        ),
        None,
    )
    selection: dict[str, Any] = {"route_option": {"model": verdict.model}, "params": {}}
    if harness:
        selection["route_option"]["harness"] = harness
    return {"route_selection": [selection], "rationale": verdict.rationale}


@dataclass
class RecordedCall:
    """One request the mock served, for post-hoc assertions."""

    prompt: str
    offered: tuple[str, ...]
    router_name: str | None
    status: int
    model: str | None
    rationale: str | None


@dataclass
class MockRouter:
    """Handle on a running mock routing service."""

    base_url: str
    calls: list[RecordedCall] = field(default_factory=list)
    #: Serve the account-level "not enabled" 404 instead of routing.
    disabled: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, call: RecordedCall) -> None:
        """Append *call* to the served-request log.

        :param call: The request/response pair just served.
        """
        with self._lock:
            self.calls.append(call)

    def reset(self) -> None:
        """Clear the served-request log."""
        with self._lock:
            self.calls.clear()

    def snapshot(self) -> list[RecordedCall]:
        """Return a stable copy of the served-request log.

        :returns: The calls served so far, oldest first.
        """
        with self._lock:
            return list(self.calls)

    def count(self) -> int:
        """Return how many requests the mock has served.

        :returns: The served-request count.
        """
        with self._lock:
            return len(self.calls)

    def calls_with(self, needle: str) -> list[RecordedCall]:
        """Return the served calls whose prompt contains *needle*.

        The suite shares one mock across a session, and a pane from an earlier
        CUJ can still be working while a later one runs. Counting only the calls
        carrying this test's own prompt text keeps "exactly one routing call" and
        "zero routing calls" honest instead of coupling them to whatever else is
        alive.

        :param needle: A distinctive fragment of the prompt under test.
        :returns: The matching calls, oldest first.
        """
        with self._lock:
            return [call for call in self.calls if needle in call.prompt]


def _handler_class(router: MockRouter) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to *router*.

    :param router: Handle the served requests are recorded on.
    :returns: A ``BaseHTTPRequestHandler`` subclass.
    """

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:
            """Silence the stdlib access log; pytest captures enough."""

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            """Serve ``routes:select``; 404 anything else."""
            if not self.path.endswith(ROUTES_SELECT_PATH):
                self._reply(404, {"error_code": "NOT_FOUND", "message": f"no route {self.path}"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                self._reply(400, {"error_code": "BAD_REQUEST", "message": "body is not JSON"})
                return
            if not isinstance(body, dict):
                self._reply(400, {"error_code": "BAD_REQUEST", "message": "body is not an object"})
                return
            offered = tuple(
                str(o.get("model") or "")
                for o in (body.get("route_options") or [])
                if isinstance(o, dict)
            )
            prompt = str((body.get("task") or {}).get("prompt") or "")
            router_name = (body.get("route_selector") or {}).get("router_name")
            if router.disabled:
                router.record(
                    RecordedCall(
                        prompt=prompt,
                        offered=offered,
                        router_name=router_name,
                        status=404,
                        model=None,
                        rationale=None,
                    )
                )
                self._reply(404, {"error_code": "NOT_FOUND", "message": NOT_ENABLED_MESSAGE})
                return
            try:
                payload = select_route(body)
            except MenuRejected as exc:
                router.record(
                    RecordedCall(
                        prompt=prompt,
                        offered=offered,
                        router_name=router_name,
                        status=400,
                        model=None,
                        rationale=None,
                    )
                )
                self._reply(400, {"error_code": "BAD_REQUEST", "message": exc.message})
                return
            router.record(
                RecordedCall(
                    prompt=prompt,
                    offered=offered,
                    router_name=router_name,
                    status=200,
                    model=payload["route_selection"][0]["route_option"]["model"],
                    rationale=payload["rationale"],
                )
            )
            self._reply(200, payload)

    return _Handler


def serve_mock_router() -> Iterator[MockRouter]:
    """Run the mock routing service on a loopback port for the caller's scope.

    :yields: The :class:`MockRouter` handle, whose ``base_url`` goes straight
        into a server config's ``routing.base_url``.
    """
    router = MockRouter(base_url="")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler_class(router))
    httpd.daemon_threads = True
    host, port = httpd.server_address[0], httpd.server_address[1]
    router.base_url = f"http://{host}:{port}{BASE_PATH}"
    thread = threading.Thread(target=httpd.serve_forever, name="mock-routes-select", daemon=True)
    thread.start()
    try:
        yield router
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
