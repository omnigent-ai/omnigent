"""HTTP client for the sandbox-control API.

Keeps every wire detail — routes, envelopes, timeout classes — out of the
launcher so it can be tested without omnigent installed.
"""

from __future__ import annotations

import functools
import secrets
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

import httpx
from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

if TYPE_CHECKING:
    from omnigent.community.sandbox.remote.config import RemoteSandboxConfig

# Two timeout classes, chosen per route. A route the API documents as
# blocking answers only once the work it names has finished, so a response
# read deadline would abandon work still in flight. Connection setup, pool
# acquisition, and request writes must still fail within the configured time.
USER_EMAIL_HEADER = "X-Omnigent-User-Email"


class RemoteSandboxError(RuntimeError):
    """A sandbox-control API call failed, or a sandbox ended in a failure state.

    :param status_code: The HTTP status the service answered with, or ``None``
        when the call never got a response (or failed on its body).
    :param transport_failure: Whether the request failed before a response
        arrived, e.g. a connection reset.
    :param code: The service's error code from its ``{"error": {"code"}}``
        body, e.g. ``"SANDBOX_GONE"``, when it sent one.

    Raised inside a trace, the message ends with ``(trace_id=<hex>)``: the
    ID the sandbox API's spans, logs, and error bodies carry for the call.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        transport_failure: bool = False,
        code: str | None = None,
    ) -> None:
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            message = f"{message} (trace_id={trace.format_trace_id(span_context.trace_id)})"
        super().__init__(message)
        self.status_code = status_code
        self.transport_failure = transport_failure
        self.code = code

    @property
    def retryable(self) -> bool:
        """Whether repeating the same call could succeed.

        A lost connection and a 500, 502, or 503 are transient. A 504 is not:
        the service already waited its full bound for the sandbox.
        """
        return self.transport_failure or self.status_code in (500, 502, 503)


_P = ParamSpec("_P")
_R = TypeVar("_R")
_TRACE_CONTEXT = TraceContextTextMapPropagator()


def traced(method: Callable[_P, _R]) -> Callable[_P, _R]:
    """Run *method* under :func:`sandbox_trace`, joining any active trace."""

    @functools.wraps(method)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with sandbox_trace():
            return method(*args, **kwargs)

    return wrapper


class RemoteSandboxClient:
    """Synchronous client for the sandbox-control API.

    Methods are blocking and thread-safe (each call builds its own request);
    omnigent runs launcher methods in worker threads.

    :param config: The provider's ``sandbox.remote`` block.
    :param api_key: Key sent on every request.
    :param principal: Authenticated Omnigent account username. The historical
        wire header calls this an email, but accounts mode deliberately keeps
        usernames opaque and does not require mailbox proof. ``None`` sends no
        user header, for the API-key-only ``/v1/capabilities`` read.
    :param transport: Override for the underlying httpx transport, for tests.
    """

    def __init__(
        self,
        config: RemoteSandboxConfig,
        api_key: str,
        principal: str | None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Api-Key {api_key}"}
        if principal is not None:
            principal = principal.strip()
            if not principal:
                raise RemoteSandboxError(
                    "remote sandbox requests require an authenticated principal"
                )
            headers[USER_EMAIL_HEADER] = principal
        self.principal = principal
        self._http = httpx.Client(
            base_url=config.api_url.rstrip("/"),
            headers=headers,
            timeout=config.request_timeout_s,
            transport=transport,
        )
        self._blocking_timeout = httpx.Timeout(config.request_timeout_s, read=None)

    # -- control routes ------------------------------------------------------

    @traced
    def health(self) -> None:
        self._request("GET", "/health")

    @traced
    def capabilities(self) -> dict[str, Any]:
        return self._object("GET", "/v1/capabilities")

    @traced
    def check_access(self) -> bool:
        """Return whether the control plane permits this principal.

        Any transport, status, or response-shape failure raises
        :class:`RemoteSandboxError`; callers must fail closed.
        """
        if self.principal is None:
            raise RemoteSandboxError("remote sandbox requests require an authenticated principal")
        payload = self._object("POST", "/v1/access/check", json={"email": self.principal})
        return payload.get("allowed") is True

    @traced
    def list_repos(self) -> tuple[list[dict[str, Any]], bool] | None:
        """Return ``(repos, truncated)`` for the new-session repo picker.

        ``None`` when the service predates ``GET /v1/repos`` (a 404), so the
        caller can tell "no listing" apart from an empty one. Each repo is
        ``{full_name, clone_url, default_branch, private, pushed_at}``.
        """
        response = self._request("GET", "/v1/repos", tolerate=(404,))
        if response.status_code == 404:
            return None
        payload = self._decode(response, "GET", "/v1/repos")
        raw = payload.get("repos")
        if not isinstance(raw, list):
            raise RemoteSandboxError(f"GET /v1/repos returned no repos list: {payload!r}")
        return [_repo(entry) for entry in raw], payload.get("truncated") is True

    # -- static sandboxes (the caller's own long-lived VMs) -------------------

    @traced
    def list_available_vms(self) -> list[dict[str, Any]]:
        """Every VM the principal owns that can be added as a static sandbox.

        Each entry is ``{sandbox_id, status, repo, last_activity_at}``; the
        service already leaves out sandboxes Omnigent itself created.
        """
        path = "/v1/vms/available"
        return [_vm(entry, path) for entry in self._list("GET", path, "vms")]

    @traced
    def list_static_sandboxes(self) -> list[dict[str, Any]]:
        """The principal's saved static sandboxes, with live VM status.

        Each entry is ``{sandbox_id, label, status, repo, default_repo_url,
        checkout_path, last_activity_at, created_at, updated_at,
        omnigent_host_id}``; ``status`` is ``running``, ``paused``, or
        ``gone``, ``omnigent_host_id`` is the linked host or ``None``, and the
        repo fields are ``""`` when the VM has no primary repo.
        """
        path = "/v1/static-sandboxes"
        return [
            _static_sandbox(entry, path) for entry in self._list("GET", path, "static_sandboxes")
        ]

    @traced
    def get_static_sandbox(self, sandbox_id: str) -> dict[str, Any] | None:
        """The principal's entry for *sandbox_id*, or ``None`` when it is not theirs."""
        path = f"/v1/static-sandboxes/{_path_segment(sandbox_id)}"
        response = self._request("GET", path, tolerate=(404,))
        if response.status_code == 404:
            return None
        return _static_sandbox(self._decode(response, "GET", path), path)

    @traced
    def static_checkout_path(self, sandbox_id: str, repo_url: str) -> str:
        """Where the VM keeps its checkout of *repo_url*; the service runs nothing on it."""
        path = f"/v1/static-sandboxes/{_path_segment(sandbox_id)}/checkout"
        payload = self._object("GET", path, params={"repo_url": repo_url})
        return _text(payload, "path", f"GET {path}")

    @traced
    def resume_static_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        """Wake a paused static sandbox and return its entry once it is running.

        The service waits for the VM itself, so the read deadline is lifted.
        It answers 409 ``SANDBOX_GONE`` for a VM that no longer exists and 504
        ``SANDBOX_RESUME_TIMEOUT`` when the VM did not come up in its bound.
        """
        path = f"/v1/static-sandboxes/{_path_segment(sandbox_id)}/resume"
        payload = self._object("POST", path, timeout=self._blocking_timeout)
        return _static_sandbox(payload, path)

    @traced
    def diagnose_static_host(self, sandbox_id: str, server_url: str) -> dict[str, Any]:
        """Report why the VM's ``omnigent host`` may not be connecting to *server_url*.

        Returns ``{sandbox_id, status, host_process_running, omnigent_on_path,
        omnigent_version, server_host, server_resolves, log_tail,
        fatal_reason}``; ``fatal_reason`` is non-empty when the host can never
        come online. The service answers 409 for a paused or gone VM.
        """
        path = f"/v1/static-sandboxes/{_path_segment(sandbox_id)}/diagnostics"
        payload = self._object(
            "POST", path, json={"server_url": server_url}, timeout=self._blocking_timeout
        )
        return _diagnostics(payload, path)

    @traced
    def put_static_sandbox(self, sandbox_id: str, label: str) -> dict[str, Any]:
        """Add *sandbox_id* as a static sandbox, or rename it, and return the entry.

        The service answers 404 when the principal has no live VM by that name
        and 400 for a name it refuses (e.g. one of Omnigent's own sandboxes).
        """
        path = f"/v1/static-sandboxes/{_path_segment(sandbox_id)}"
        return _static_sandbox(self._object("PUT", path, json={"label": label}), path)

    @traced
    def link_static_host(self, sandbox_id: str, host_id: str) -> dict[str, Any]:
        """Link an Omnigent host to a static sandbox, if it has none yet.

        The service stores the link only when the entry's link is empty, so the
        returned entry carries the winning ``omnigent_host_id``: concurrent
        callers on different replicas all converge on one host.
        """
        path = f"/v1/static-sandboxes/{_path_segment(sandbox_id)}/host"
        payload = self._object("PUT", path, json={"omnigent_host_id": host_id})
        return _static_sandbox(payload, path)

    @traced
    def delete_static_sandbox(self, sandbox_id: str) -> bool:
        """Remove a static-sandbox entry; the VM itself is never touched.

        :returns: Whether an entry existed.
        """
        path = f"/v1/static-sandboxes/{_path_segment(sandbox_id)}"
        return self._request("DELETE", path, tolerate=(404,)).status_code != 404

    def _list(self, method: str, path: str, key: str, **kwargs: Any) -> list[object]:
        payload = self._object(method, path, **kwargs)
        raw = payload.get(key)
        if not isinstance(raw, list):
            raise RemoteSandboxError(f"{method} {path} returned no {key} list: {payload!r}")
        return raw

    def close(self) -> None:
        """Release the underlying connection pool before rebinding identity."""
        self._http.close()

    @traced
    def reserve(self, name: str) -> str:
        """Reserve a sandbox id. The service derives it; callers treat it as opaque."""
        payload = self._object("POST", "/v1/sandboxes", json={"name": name})
        return _text(payload, "sandbox_id", "POST /v1/sandboxes")

    @traced
    def state(self, sandbox_id: str) -> str:
        payload = self._object("GET", f"/v1/sandboxes/{sandbox_id}")
        return _text(payload, "state", f"sandbox {sandbox_id!r}")

    @traced
    def delete(self, sandbox_id: str, *, capture_failure_log: bool = False) -> None:
        """Delete a sandbox, treating one that is already absent as success.

        ``SandboxLifecycle.terminate`` requires idempotence so cleanup can
        retry after a crash; without it teardown never marks the sandbox
        terminated and the reaper keeps retrying a finished cleanup.
        """
        kwargs = {"params": {"capture_failure_log": "true"}} if capture_failure_log else {}
        self._request("DELETE", f"/v1/sandboxes/{sandbox_id}", tolerate=(404,), **kwargs)

    @traced
    def submit_exec(self, sandbox_id: str, script: str) -> str:
        """Submit a script and return the id its result is collected under.

        Submit and collect are separate calls so a client timeout mid-run
        still leaves the caller able to name the work it started.
        """
        payload = self._object("POST", f"/v1/sandboxes/{sandbox_id}/exec", json={"script": script})
        return _text(payload, "exec_id", "exec submit")

    # -- blocking routes -----------------------------------------------------

    @traced
    def start(
        self,
        sandbox_id: str,
        *,
        repo_url: str | None,
        repo_branch: str | None,
        repos: Sequence[tuple[str, str | None]] = (),
    ) -> None:
        """Materialize the reserved sandbox and wait until it is usable.

        *repo_url* / *repo_branch* name the first repo; *repos* carries the
        full ``(url, branch)`` list and is only sent when there are several.
        """
        body: dict[str, Any] = {"repo_url": repo_url, "repo_branch": repo_branch}
        if len(repos) > 1:
            body["repos"] = [{"url": url, "branch": branch} for url, branch in repos]
        self._ready(sandbox_id, "start", body)

    @traced
    def resume(self, sandbox_id: str) -> None:
        self._ready(sandbox_id, "resume", {})

    @traced
    def workspace(
        self,
        sandbox_id: str,
        *,
        repo_url: str,
        repo_branch: str | None,
        repo_name: str | None,
    ) -> str:
        """Materialize the repository and return the path the service put it at."""
        payload = self._object(
            "POST",
            f"/v1/sandboxes/{sandbox_id}/workspace",
            json={"repo_url": repo_url, "repo_branch": repo_branch, "repo_name": repo_name},
            timeout=self._blocking_timeout,
        )
        return _text(payload, "workspace_path", "workspace")

    @traced
    def exec_result(self, sandbox_id: str, exec_id: str) -> tuple[int, str, str]:
        """Wait for a submitted script to finish and return its outcome.

        The API offers one merged output stream, so ``stdout`` carries the
        script's combined output and ``stderr`` carries the service's own
        explanation when a script fails. Both are passed through as they
        arrive; nothing here reads the split as two process streams.

        :returns: Exit code, combined output, and the service's explanation.
        """
        payload = self._object(
            "GET",
            f"/v1/sandboxes/{sandbox_id}/exec/{exec_id}/result",
            timeout=self._blocking_timeout,
        )
        exit_code = payload.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise RemoteSandboxError(f"exec {exec_id} reported no exit_code: {payload!r}")
        return exit_code, str(payload.get("stdout") or ""), str(payload.get("stderr") or "")

    @traced
    def run_script(self, sandbox_id: str, script: str) -> tuple[int, str, str]:
        """Submit a script and block until the service reports its outcome."""
        return self.exec_result(sandbox_id, self.submit_exec(sandbox_id, script))

    # -- plumbing ------------------------------------------------------------

    def _ready(self, sandbox_id: str, route: str, body: dict[str, Any]) -> None:
        payload = self._object(
            "POST",
            f"/v1/sandboxes/{sandbox_id}/{route}",
            json=body,
            timeout=self._blocking_timeout,
        )
        if payload.get("ready") is not True:
            raise RemoteSandboxError(f"sandbox {sandbox_id!r} is not ready: {payload!r}")

    def _request(
        self, method: str, path: str, *, tolerate: tuple[int, ...] = (), **kwargs: Any
    ) -> httpx.Response:
        """Issue a request, raising on any status at or above 400.

        :param tolerate: Status codes to return rather than raise on, for
            routes where a 4xx is a legitimate outcome.
        """
        headers = dict(kwargs.pop("headers", None) or {})
        propagate.inject(headers)
        # The sandbox API needs W3C trace context whatever OTEL_PROPAGATORS says.
        _TRACE_CONTEXT.inject(headers)
        try:
            response = self._http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise RemoteSandboxError(
                f"{method} {path} failed: {exc}", transport_failure=True
            ) from exc
        if response.status_code >= 400 and response.status_code not in tolerate:
            raise RemoteSandboxError(
                f"{method} {path} returned HTTP {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
                code=_error_code(response),
            )
        return response

    def _object(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Issue a request whose body must decode to a JSON object.

        The launcher's handlers catch only :class:`RemoteSandboxError`, so a
        2xx carrying an empty or non-JSON body must not surface as a raw
        ``ValueError``: it would escape every handler and break
        ``is_running``'s documented tri-state.
        """
        return self._decode(self._request(method, path, **kwargs), method, path)

    @staticmethod
    def _decode(response: httpx.Response, method: str, path: str) -> dict[str, Any]:
        """Decode a response body that must be a JSON object."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise RemoteSandboxError(
                f"{method} {path} returned HTTP {response.status_code} with an "
                f"undecodable body: {response.text[:500]}"
            ) from exc
        if not isinstance(payload, dict):
            raise RemoteSandboxError(
                f"{method} {path} returned HTTP {response.status_code} with a "
                f"non-object body: {payload!r}"
            )
        return payload


@contextmanager
def sandbox_trace() -> Iterator[str]:
    """Run a block under one trace and yield its W3C trace ID.

    Joins the active span when there is one. Otherwise a new sampled
    context is made current for the block. Every request inside it sends a
    ``traceparent`` with that ID, a :class:`RemoteSandboxError` quotes it,
    and omnigent log lines written inside it carry it. The sandbox API's
    spans, logs, and error bodies carry the same ID.
    """
    current = trace.get_current_span().get_span_context()
    if current.is_valid:
        yield trace.format_trace_id(current.trace_id)
        return
    minted = trace.SpanContext(
        trace_id=int.from_bytes(secrets.token_bytes(16), "big") or 1,
        span_id=int.from_bytes(secrets.token_bytes(8), "big") or 1,
        is_remote=True,
        trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
    )
    token = otel_context.attach(trace.set_span_in_context(trace.NonRecordingSpan(minted)))
    try:
        yield trace.format_trace_id(minted.trace_id)
    finally:
        otel_context.detach(token)


def _repo(entry: object) -> dict[str, Any]:
    """Validate one ``GET /v1/repos`` entry into the picker's repo shape."""
    if not isinstance(entry, dict):
        raise RemoteSandboxError(f"GET /v1/repos returned a non-object repo: {entry!r}")
    full_name = _text(entry, "full_name", "GET /v1/repos")
    clone_url = _text(entry, "clone_url", "GET /v1/repos")
    default_branch = entry.get("default_branch")
    pushed_at = entry.get("pushed_at")
    return {
        "full_name": full_name,
        "clone_url": clone_url,
        "default_branch": default_branch if isinstance(default_branch, str) else None,
        "private": entry.get("private") is True,
        "pushed_at": pushed_at if isinstance(pushed_at, str) else None,
    }


def _path_segment(value: str) -> str:
    """Quote one URL path segment so a name can never address another route."""
    if not value or value in (".", ".."):
        raise RemoteSandboxError(f"invalid sandbox name {value!r}")
    return urllib.parse.quote(value, safe="")


def _optional_text(entry: dict[str, Any], key: str) -> str | None:
    value = entry.get(key)
    return value if isinstance(value, str) and value else None


def _error_code(response: httpx.Response) -> str | None:
    """The ``error.code`` of an error body, when it has one."""
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code else None


_STATIC_STATUSES = ("running", "paused", "gone")


def _status(entry: dict[str, Any]) -> str:
    """The entry's ``running`` / ``paused`` / ``gone``; anything else reads as paused."""
    status = entry.get("status")
    return status if status in _STATIC_STATUSES else "paused"


def _vm(entry: object, what: str) -> dict[str, Any]:
    """Validate one ``GET /v1/vms/available`` entry."""
    if not isinstance(entry, dict):
        raise RemoteSandboxError(f"{what} returned a non-object entry: {entry!r}")
    return {
        "sandbox_id": _text(entry, "sandbox_id", what),
        "status": _status(entry),
        "repo": _optional_text(entry, "repo") or "",
        "last_activity_at": _optional_text(entry, "last_activity_at"),
    }


def _static_sandbox(entry: object, what: str) -> dict[str, Any]:
    """Validate one static-sandbox entry; the label falls back to the sandbox id."""
    vm = _vm(entry, what)
    assert isinstance(entry, dict)
    return {
        **vm,
        "label": _optional_text(entry, "label") or vm["sandbox_id"],
        "default_repo_url": _optional_text(entry, "default_repo_url") or "",
        "checkout_path": _optional_text(entry, "checkout_path") or "",
        "created_at": _optional_text(entry, "created_at"),
        "updated_at": _optional_text(entry, "updated_at"),
        "omnigent_host_id": _optional_text(entry, "omnigent_host_id"),
    }


def _optional_bool(entry: dict[str, Any], key: str) -> bool | None:
    value = entry.get(key)
    return value if isinstance(value, bool) else None


def _diagnostics(payload: dict[str, Any], what: str) -> dict[str, Any]:
    """Validate a static-sandbox diagnostics report."""
    return {
        "sandbox_id": _text(payload, "sandbox_id", what),
        "status": _status(payload),
        "host_process_running": _optional_bool(payload, "host_process_running"),
        "omnigent_on_path": payload.get("omnigent_on_path") is True,
        "omnigent_version": _optional_text(payload, "omnigent_version") or "",
        "server_host": _optional_text(payload, "server_host") or "",
        "server_resolves": _optional_bool(payload, "server_resolves"),
        "log_tail": _optional_text(payload, "log_tail") or "",
        "fatal_reason": _optional_text(payload, "fatal_reason") or "",
    }


def _text(payload: dict[str, Any], key: str, what: str) -> str:
    """Read a required non-empty string field out of a response body."""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RemoteSandboxError(f"{what} returned no {key}: {payload!r}")
    return value
