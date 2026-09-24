"""Opt-in observations for existing synchronous pytest/Playwright drivers."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import threading
import tokenize
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from .execution import Journal, digest_file, safe_url, sanitize_trace


class Evidence:
    def __init__(self, directory: Path):
        self.directory = directory
        self.journal = Journal(directory)
        self.patch = pytest.MonkeyPatch()
        self.sessions: dict[tuple[str, str], set[str | None]] = {}
        self.contexts: dict = {}
        self.local = threading.local()
        self.sessions_lock = threading.Lock()
        self.snapshot_lock = threading.Lock()
        self.node = None

    @property
    def busy(self):
        return getattr(self.local, "busy", False)

    @busy.setter
    def busy(self, value):
        self.local.busy = value

    def emit(self, kind, **data):
        self.journal.emit(kind, test_id=self.node, **data)

    def capture(self, operation, callback):
        return self.journal.capture(operation, callback, test_id=self.node)

    def artifact(self, operation, path, callback, **data):
        def save():
            callback()
            if not path.is_file():
                raise FileNotFoundError(path)
            self.emit("artifact", path=path.name, **data)

        self.capture(operation, save)

    def session(self, url, body=None, state=None):
        parts = urlsplit(url)
        # Follow only local test servers observed by the driver.
        if parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return
        # Keep authentication for snapshot requests; emitted origins always use safe_url.
        origin = f"{parts.scheme}://{parts.netloc}"
        match = re.match(r"^/v1/sessions/([^/]+)(?:/|$)", parts.path)
        if match is None:
            match = re.fullmatch(r"/c/([^/]+)/?", parts.path)
        sid = (
            match[1]
            if match
            else body.get("id")
            if isinstance(body, dict) and parts.path == "/v1/sessions"
            else None
        )
        if sid and not sid.startswith("temp:"):
            key = (origin, sid)
            with self.sessions_lock:
                self.sessions.setdefault(key, set()).add(self.node)
                if state is not None:
                    state["sessions"].add(key)
            self.emit("product_session", server=safe_url(origin), session_id=sid)

    def snapshot(self, reason, sessions=None):
        if self.busy:
            return
        self.busy = True
        try:
            with self.snapshot_lock:
                with self.sessions_lock:
                    selected = [
                        (base, sid)
                        for base, sid in sorted(self.sessions if sessions is None else sessions)
                        if sessions is not None
                        or self.node is None
                        or self.node in self.sessions[base, sid]
                    ]
                for base, sid in selected:
                    self.capture(
                        "session_snapshot",
                        lambda base=base, sid=sid: self.read_session(base, sid, reason),
                    )
        finally:
            self.busy = False

    def read_session(self, base, sid, reason):
        with self.sessions_lock:
            observed_in_tests = sorted(n for n in self.sessions[base, sid] if n)
        with httpx.Client(trust_env=False, timeout=3) as client:
            for suffix in ("", "/resources"):
                r = client.get(f"{base}/v1/sessions/{sid}{suffix}")
                self.emit(
                    "session_snapshot",
                    session_id=sid,
                    server=safe_url(base),
                    reason=reason,
                    observed_in_tests=observed_in_tests,
                    surface=suffix or "info",
                    status=r.status_code,
                    body=r.json() if r.is_success else None,
                )
            after = None
            for _ in range(20):
                params = {"limit": 100, "order": "asc"}
                if after:
                    params["after"] = after
                r = client.get(f"{base}/v1/sessions/{sid}/items", params=params)
                r.raise_for_status()
                body = r.json()
                self.emit(
                    "session_items",
                    server=safe_url(base),
                    session_id=sid,
                    reason=reason,
                    body=body,
                )
                if not body.get("has_more"):
                    return
                rows = body.get("data", [])
                next_after = rows[-1].get("id") if rows else None
                if not next_after or next_after == after:
                    break
                after = next_after
            self.emit("collection_incomplete", operation="session_items", session_id=sid)

    def install_http(self):
        send = httpx.Client.send

        def observed(client, request, *args, **kwargs):
            if self.busy or request.url.host not in {"localhost", "127.0.0.1", "::1"}:
                return send(client, request, *args, **kwargs)
            path = request.url.path
            if request.method == "DELETE" and path.startswith("/v1/sessions/"):
                self.session(str(request.url))
                self.snapshot("before_session_delete")
            if path == "/mock/reset":
                self.busy = True
                try:
                    self.capture(
                        "before_mock_reset",
                        lambda: self.emit(
                            "mock_requests",
                            server=safe_url(str(request.url.copy_with(path="/"))),
                            body=client.get(request.url.copy_with(path="/mock/requests")).json(),
                        ),
                    )
                finally:
                    self.busy = False
            response = send(client, request, *args, **kwargs)
            if path.startswith(("/v1/sessions", "/mock/")):

                def record():
                    body = None
                    unavailable = {}
                    if kwargs.get("stream"):
                        unavailable["response"] = "streamed"
                    elif "json" in response.headers.get("content-type", ""):
                        try:
                            body = response.json()
                        except ValueError:
                            unavailable["response"] = "invalid_json"
                    try:
                        request_body = request.content.decode(errors="replace")
                    except httpx.RequestNotRead:
                        request_body = None
                        unavailable["request"] = "streamed"
                    self.session(str(request.url), body)
                    self.emit(
                        "http",
                        method=request.method,
                        url=safe_url(str(request.url)),
                        status=response.status_code,
                        request=request_body,
                        response=body,
                        body_unavailable=unavailable,
                    )

                self.capture("http", record)
            return response

        self.patch.setattr(httpx.Client, "send", observed)

    def install_browser(self):
        from playwright.sync_api import Browser, BrowserContext, Page, Route

        create, close, fulfill = Browser.new_context, BrowserContext.close, Route.fulfill

        def save_trace(context, state):
            state["tracing"] = False
            path = self.directory / f"trace-{state['id']}.zip"

            def save():
                # Raw traces never enter the directory retained by the workflow.
                with tempfile.TemporaryDirectory(prefix="repro-raw-trace-") as temporary:
                    raw = Path(temporary) / "trace.zip"
                    context.tracing.stop(path=str(raw))
                    sanitize_trace(raw, self.journal.secrets)
                    shutil.copyfile(raw, path)

            self.artifact(
                "trace_stop",
                path,
                save,
                context_id=state["id"],
                created_in_test=state["created_in_test"],
                kind_of_artifact="playwright_trace",
            )

        def new_context(browser, *args, **kwargs):
            context = create(browser, *args, **kwargs)
            key = uuid.uuid4().hex
            state = {
                "id": key,
                "tracing": False,
                "pages": [],
                "sessions": set(),
                "created_in_test": self.node,
            }
            self.contexts[context] = state

            def start_trace():
                context.tracing.start(screenshots=True, snapshots=True, sources=False)
                state["tracing"] = True

            self.capture("trace_start", start_trace)
            start = context.tracing.start

            def caller_start(*args, **kwargs):
                if state["tracing"]:
                    save_trace(context, state)
                    self.emit(
                        "trace_owner",
                        context_id=state["id"],
                        owner="driver",
                        limitation="Subsequent tracing is owned and retained by the driver.",
                    )
                return start(*args, **kwargs)

            self.patch.setattr(context.tracing, "start", caller_start)
            context.on(
                "page", lambda page: self.capture("browser_page", lambda: self.page(page, state))
            )
            context.on(
                "response",
                lambda response: self.capture(
                    "browser_response", lambda: self.response(response, key, state)
                ),
            )
            self.emit(
                "browser_context",
                context_id=key,
                coverage="sync Playwright trace/network/fulfill; other clients unknown",
            )
            return context

        def close_context(context, *args, **kwargs):
            state = self.contexts.pop(context, None)
            if state:
                self.snapshot("before_browser_close", sessions=state["sessions"])
                for page in state["pages"]:
                    if not page.is_closed():
                        path = self.directory / f"screen-{uuid.uuid4().hex}.png"
                        self.artifact(
                            "screenshot",
                            path,
                            lambda page=page, path=path: page.screenshot(path=str(path)),
                            context_id=state["id"],
                            created_in_test=state["created_in_test"],
                            kind_of_artifact="screenshot",
                            url=safe_url(page.url),
                        )
                if state["tracing"]:
                    save_trace(context, state)
            result = close(context, *args, **kwargs)
            if state:
                for page in state["pages"]:
                    if page.video:
                        path = self.directory / f"video-{uuid.uuid4().hex}.webm"
                        self.artifact(
                            "video",
                            path,
                            lambda page=page, path=path: page.video.save_as(str(path)),
                            context_id=state["id"],
                            created_in_test=state["created_in_test"],
                            kind_of_artifact="video",
                        )
            return result

        def fulfilled(route, *args, **kwargs):
            context_id = self.capture(
                "route_context",
                lambda: self.contexts.get(route.request.frame.page.context, {}).get("id"),
            )
            result = fulfill(route, *args, **kwargs)

            def record_fulfillment():
                source = kwargs.get("path")
                response = kwargs.get("response")
                self.emit(
                    "browser_fulfill",
                    context_id=context_id,
                    url=safe_url(route.request.url),
                    boundary="browser response",
                    body=kwargs.get("body"),
                    json=kwargs.get("json"),
                    status=kwargs.get("status"),
                    path=str(source) if source is not None else None,
                    source_sha256=self.capture("fulfill_source", lambda: digest_file(Path(source)))
                    if source is not None
                    else None,
                    response_source={"url": safe_url(response.url), "status": response.status}
                    if response is not None
                    else None,
                )

            self.capture("browser_fulfill", record_fulfillment)
            return result

        self.patch.setattr(Browser, "new_context", new_context)
        self.patch.setattr(BrowserContext, "close", close_context)
        self.patch.setattr(Route, "fulfill", fulfilled)
        for cls in (Page, BrowserContext):
            original = cls.route

            def registered(obj, url, handler, *args, _original=original, **kwargs):
                self.emit(
                    "browser_route_registered", pattern=str(url), boundary="browser response"
                )
                return _original(obj, url, handler, *args, **kwargs)

            self.patch.setattr(cls, "route", registered)

    def page(self, page, state):
        state["pages"].append(page)

        def websocket(ws):
            self.emit("websocket_open", context_id=state["id"], url=safe_url(ws.url))
            for event in ("framesent", "framereceived"):
                ws.on(
                    event,
                    lambda frame, event=event: self.emit(
                        "websocket_frame",
                        context_id=state["id"],
                        url=safe_url(ws.url),
                        direction=event,
                        payload=frame.decode(errors="replace")
                        if isinstance(frame, bytes)
                        else frame,
                    ),
                )

        page.on("websocket", websocket)
        page.on(
            "framenavigated",
            lambda frame: self.capture("navigation", lambda: self.navigation(frame, state)),
        )

    def navigation(self, frame, state):
        self.session(frame.url, state=state)
        self.emit("navigation", context_id=state["id"], url=safe_url(frame.url))

    def response(self, response, context_id, state=None):
        request = response.request
        parts = urlsplit(response.url)
        if parts.hostname not in {"127.0.0.1", "localhost", "::1"} or not parts.path.startswith(
            "/v1/"
        ):
            return
        body = response.json() if "json" in response.headers.get("content-type", "") else None
        self.session(response.url, body, state=state)
        self.emit(
            "browser_response",
            context_id=context_id,
            url=safe_url(response.url),
            method=request.method,
            status=response.status,
            request=request.post_data,
            response=body,
            provenance="browser-observed; inspect route/fulfill events for substitutions",
        )


def pytest_configure(config):
    path = os.environ.get("OMNIGENT_REPRO_ATTEMPT_DIR")
    if not path:
        return
    collector = Evidence(Path(path))
    config._repro_evidence = collector
    collector.install_http()
    collector.capture("playwright_install", collector.install_browser)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item):
    collector = getattr(item.config, "_repro_evidence", None)
    if collector:
        collector.node = f"{item.nodeid}:{uuid.uuid4().hex}"
        collector.emit("test_start", nodeid=item.nodeid)
        source = Path(str(item.path))

        def copy_source():
            with tokenize.open(source) as stream:
                content = collector.journal.clean(stream.read()).encode("utf-8")
            target = collector.directory / f"source-{hashlib.sha256(content).hexdigest()}.py"
            if not target.is_file():
                target.write_bytes(content)
            collector.emit(
                "artifact", path=target.name, source=str(source), kind_of_artifact="test_source"
            )

        collector.capture("test_source", copy_source)
    yield
    if collector:
        collector.emit("test_end", nodeid=item.nodeid)
        collector.node = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    yield
    collector = getattr(item.config, "_repro_evidence", None)
    if collector:
        collector.snapshot("after_test_before_teardown")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item):
    result = yield
    collector = getattr(item.config, "_repro_evidence", None)
    if collector:
        report = result.get_result()
        collector.emit(
            "test_result",
            stage=report.when,
            outcome=report.outcome,
            duration=report.duration,
            detail=str(report.longrepr) if report.failed else None,
        )


def pytest_unconfigure(config):
    collector = getattr(config, "_repro_evidence", None)
    if collector:
        for context in list(collector.contexts):
            collector.capture("remaining_context_close", context.close)
        collector.patch.undo()
