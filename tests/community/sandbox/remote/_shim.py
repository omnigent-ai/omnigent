"""Scriptable in-memory sandbox-control API, shared by these tests."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from omnigent.community.sandbox.remote.client import USER_EMAIL_HEADER, RemoteSandboxClient
from omnigent.community.sandbox.remote.config import RemoteSandboxConfig

_SANDBOX = re.compile(r"^/v1/sandboxes/[^/]+$")
_READY = re.compile(r"^/v1/sandboxes/[^/]+/(?:start|resume)$")
_EXEC_SUBMIT = re.compile(r"^/v1/sandboxes/[^/]+/exec$")
_EXEC_RESULT = re.compile(r"^/v1/sandboxes/[^/]+/exec/[^/]+/result$")
_STATIC_SANDBOX = re.compile(r"^/v1/static-sandboxes/([^/]+)$")
_STATIC_SUB = re.compile(r"^/v1/static-sandboxes/([^/]+)/(host|checkout|resume|diagnostics)$")


def _error(status: int, code: str, message: str = "") -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": code, "message": message or code, "trace_id": "t"}}
    )


class FakeService:
    """Scriptable in-memory sandbox-control API."""

    def __init__(self) -> None:
        self.state = "running"
        self.declared_capabilities: dict[str, Any] = {
            "resume_stopped": True,
            "file_copy": False,
            "streaming_exec": False,
            "foreground_exec": False,
            "local_port_forward": False,
        }
        self.workspace_path = "/srv/checkout/repo"
        # Per-repo_url answers for the workspace route; others get workspace_path.
        self.workspace_paths: dict[str, str] = {}
        self.exit_code = 0
        # One merged stream plus the service's own explanation on failure.
        self.stdout = "OMNIGENT_SANDBOX_HOME=/home/dev\n"
        self.explanation = ""
        self.delete_status = 204
        self.ready_status = 200
        # Statuses answered, in order, before ready_status applies.
        self.ready_failures: list[int] = []
        self.state_status = 200
        self.health_status = 200
        self.capabilities_status = 200
        self.access_status = 200
        self.access_allowed = True
        # None makes GET /v1/repos a 404, as on a service without the route.
        self.repos_payload: dict[str, Any] | None = {"repos": [], "truncated": False}
        self.repos_status = 200
        # Paths that arrived without a usable API key.
        self.unauthenticated: list[str] = []
        # (path, authenticated account username) for every request.
        self.principals: list[tuple[str, str | None]] = []
        # (method, path, body, timeout phases) for every request seen.
        self.requests: list[tuple[str, str, dict[str, Any] | None, dict[str, float | None]]] = []
        self.last_query: dict[str, str] = {}
        self.request_queries: list[dict[str, str]] = []
        # The principal's own persistent VMs, by sandbox id -> running|paused.
        self.vms: dict[str, str] = {}
        # Saved static-sandbox labels, by sandbox id.
        self.static_labels: dict[str, str] = {}
        # Linked Omnigent host ids, by sandbox id (absent = no link).
        self.static_hosts: dict[str, str] = {}
        # Every static sandbox's primary GitHub repo ("" for none).
        self.static_repo = "example/sample-app"
        # Non-200 answer for the static resume route, e.g. (504, code).
        self.resume_error: tuple[int, str] | None = None
        # The diagnostics report's fields, over a healthy default.
        self.diagnostics: dict[str, Any] = {}
        self.diagnostics_error: tuple[int, str] | None = None
        # Per-sandbox state overrides for GET /v1/sandboxes/{id}; else ``state``.
        self.states: dict[str, str] = {}
        # Non-200 status for every static-sandbox route, e.g. 503.
        self.static_status = 200

    def static_entry(self, sandbox_id: str) -> dict[str, Any]:
        repo = self.static_repo
        return {
            "sandbox_id": sandbox_id,
            "label": self.static_labels[sandbox_id],
            "status": self.vms.get(sandbox_id, "gone"),
            "repo": repo,
            "default_repo_url": f"https://github.com/{repo}" if repo else "",
            "checkout_path": f"/workspaces/{repo.rsplit('/', 1)[-1].lower()}" if repo else "",
            "last_activity_at": None,
            "created_at": "2026-10-08T00:00:00Z",
            "updated_at": "2026-10-08T00:00:00Z",
            "omnigent_host_id": self.static_hosts.get(sandbox_id),
        }

    def diagnostics_report(self, sandbox_id: str, server_url: str) -> dict[str, Any]:
        report: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "status": "running",
            "host_process_running": True,
            "omnigent_on_path": True,
            "omnigent_version": "0.17.0",
            "server_host": httpx.URL(server_url).host,
            "server_resolves": True,
            "log_tail": "",
            "fatal_reason": "",
        }
        return {**report, **self.diagnostics}

    def _static_route(
        self, method: str, sandbox_id: str, sub: str, body: dict[str, Any] | None
    ) -> httpx.Response:
        if sub == "host" and method == "PUT":
            assert body is not None
            if sandbox_id not in self.static_labels:
                return _error(404, "NOT_FOUND", "no entry")
            # Link only if empty, so concurrent callers converge on one id.
            self.static_hosts.setdefault(sandbox_id, body["omnigent_host_id"])
            return httpx.Response(200, json=self.static_entry(sandbox_id))
        if sub == "checkout" and method == "GET":
            repo_url = self.last_query.get("repo_url", "")
            if not repo_url:
                return _error(400, "VALIDATION", "repo_url is required")
            if sandbox_id not in self.static_labels:
                return _error(404, "NOT_FOUND", "no entry")
            name = repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower()
            return httpx.Response(200, json={"path": f"/workspaces/{name}"})
        if sub == "resume" and method == "POST":
            if sandbox_id not in self.static_labels:
                return _error(404, "NOT_FOUND", "no entry")
            if sandbox_id not in self.vms:
                return _error(409, "SANDBOX_GONE", f"VM {sandbox_id} no longer exists")
            if self.resume_error is not None:
                return _error(*self.resume_error)
            # The real shim waits here until the VM runs.
            self.vms[sandbox_id] = "running"
            return httpx.Response(200, json=self.static_entry(sandbox_id))
        if sub == "diagnostics" and method == "POST":
            if not body or not body.get("server_url"):
                return _error(400, "VALIDATION", "server_url is required")
            if sandbox_id not in self.static_labels:
                return _error(404, "NOT_FOUND", "no entry")
            if sandbox_id not in self.vms:
                return _error(409, "SANDBOX_GONE")
            if self.vms[sandbox_id] != "running":
                return _error(409, "SANDBOX_PAUSED")
            if self.diagnostics_error is not None:
                return _error(*self.diagnostics_error)
            return httpx.Response(
                200, json=self.diagnostics_report(sandbox_id, body["server_url"])
            )
        return _error(404, "NOT_FOUND", "no route")

    def paths(self, method: str | None = None) -> list[str]:
        return [p for m, p, _, _ in self.requests if method is None or m == method]

    def body(self, path: str) -> dict[str, Any] | None:
        return next(b for _, p, b, _ in self.requests if p == path)

    def scripts(self) -> list[str]:
        """Every script submitted for execution, in order."""
        return [b["script"] for m, p, b, _ in self.requests if _EXEC_SUBMIT.match(p) and b]

    def timeout(self, path: str) -> dict[str, float | None]:
        """The client timeout phases applied to the first request for *path*."""
        return next(t for _, p, _, t in self.requests if p == path)

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.requests.append((request.method, path, body, request.extensions["timeout"]))
        self.last_query = dict(request.url.params)
        self.request_queries.append(self.last_query)
        self.principals.append((path, request.headers.get(USER_EMAIL_HEADER)))
        if request.headers.get("Authorization") != "Api-Key k":
            self.unauthenticated.append(path)
            return httpx.Response(401, json={"error": {"code": "UNAUTHORIZED"}})
        if path == "/health":
            return httpx.Response(self.health_status, json={"status": "ok"})
        if path == "/v1/capabilities":
            if self.capabilities_status >= 400:
                return httpx.Response(self.capabilities_status, json={"error": {}})
            return httpx.Response(200, json=self.declared_capabilities)
        if path == "/v1/access/check" and request.method == "POST":
            if self.access_status >= 400:
                return httpx.Response(self.access_status, json={"error": {}})
            return httpx.Response(200, json={"allowed": self.access_allowed})
        if path == "/v1/repos" and request.method == "GET":
            if self.repos_payload is None:
                return httpx.Response(404, json={"error": {"message": "no route"}})
            if self.repos_status >= 400:
                return httpx.Response(self.repos_status, json={"error": {}})
            return httpx.Response(200, json=self.repos_payload)
        if path == "/v1/vms/available" and request.method == "GET":
            if self.static_status >= 400:
                return httpx.Response(self.static_status, json={"error": {}})
            vms = [
                {"sandbox_id": sid, "status": st, "repo": "", "last_activity_at": None}
                for sid, st in self.vms.items()
            ]
            return httpx.Response(200, json={"vms": vms})
        if path == "/v1/static-sandboxes" and request.method == "GET":
            if self.static_status >= 400:
                return httpx.Response(self.static_status, json={"error": {}})
            entries = [self.static_entry(sid) for sid in self.static_labels]
            if wanted := self.last_query.get("omnigent_host_id"):
                entries = [e for e in entries if e["omnigent_host_id"] == wanted]
            return httpx.Response(200, json={"static_sandboxes": entries})
        if match := _STATIC_SUB.match(path):
            if self.static_status >= 400:
                return httpx.Response(self.static_status, json={"error": {}})
            return self._static_route(request.method, match.group(1), match.group(2), body)
        if match := _STATIC_SANDBOX.match(path):
            if self.static_status >= 400:
                return httpx.Response(self.static_status, json={"error": {}})
            sandbox_id = match.group(1)
            if request.method == "GET":
                if sandbox_id not in self.static_labels:
                    return _error(404, "NOT_FOUND", "not one of yours")
                return httpx.Response(200, json=self.static_entry(sandbox_id))
            if request.method == "PUT":
                assert body is not None
                if sandbox_id.startswith("omnigent-"):
                    return httpx.Response(400, json={"error": {"message": "refused"}})
                if sandbox_id not in self.vms:
                    return httpx.Response(404, json={"error": {"message": "no such VM"}})
                self.static_labels[sandbox_id] = body.get("label") or sandbox_id
                return httpx.Response(200, json=self.static_entry(sandbox_id))
            if request.method == "DELETE":
                if self.static_labels.pop(sandbox_id, None) is None:
                    return httpx.Response(404, json={"error": {"message": "missing"}})
                return httpx.Response(204)
        if path == "/v1/sandboxes" and request.method == "POST":
            assert body is not None
            return httpx.Response(201, json={"sandbox_id": f"sbx-{body['name']}-9f2e1c"})
        if _SANDBOX.match(path):
            if request.method == "DELETE":
                if self.delete_status >= 400:
                    return httpx.Response(self.delete_status, json={"error": {"message": "gone"}})
                return httpx.Response(self.delete_status)
            if self.state_status >= 400:
                return httpx.Response(self.state_status, json={"error": {"message": "blip"}})
            sandbox_id = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"state": self.states.get(sandbox_id, self.state)})
        if path.endswith("/workspace"):
            assert body is not None
            workspace_path = self.workspace_paths.get(body["repo_url"], self.workspace_path)
            return httpx.Response(200, json={"workspace_path": workspace_path})
        if _READY.match(path):
            status = self.ready_failures.pop(0) if self.ready_failures else self.ready_status
            if status >= 400:
                return httpx.Response(status, json={"error": {"message": "no capacity"}})
            return httpx.Response(200, json={"ready": True})
        if _EXEC_SUBMIT.match(path):
            # A paused static sandbox must be resumed through its own route first.
            if self.vms.get(path.split("/")[3]) == "paused":
                return _error(409, "SANDBOX_PAUSED")
            return httpx.Response(202, json={"exec_id": "exec-1"})
        if _EXEC_RESULT.match(path):
            return httpx.Response(
                200,
                json={
                    "exit_code": self.exit_code,
                    "stdout": self.stdout,
                    "stderr": self.explanation,
                },
            )
        return httpx.Response(404, json={"error": {"message": f"no route {path}"}})


def make_config(**overrides: Any) -> RemoteSandboxConfig:
    return RemoteSandboxConfig(api_url="http://sandbox.test", **overrides)


def make_client(
    service: FakeService,
    config: RemoteSandboxConfig | None = None,
    *,
    principal: str | None = "alice",
) -> RemoteSandboxClient:
    config = config or make_config()
    return RemoteSandboxClient(
        config,
        "k",
        principal,
        transport=httpx.MockTransport(service.handler),
    )
