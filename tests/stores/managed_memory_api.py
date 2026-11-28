"""In-memory REST boundary for tests using the real Databricks SDK."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import requests
import responses
from databricks.sdk import WorkspaceClient

from omnigent.stores.memory_store.databricks_client import DatabricksManagedMemoryClient

HOST = "https://memory.example.test"
STORE_ID = "support-memory"
STORE_NAME = f"memory-stores/{STORE_ID}"
SERVICE_TOKEN = "memory-service-test-token"


class ManagedMemoryAPI:
    """Only the store and entry operations used by the OSS adapter."""

    def __init__(self, http: responses.RequestsMock) -> None:
        self.entries: dict[str, dict[str, object]] = {}
        self.requests: list[dict[str, object]] = []
        self.permission = "WRITE"
        self.missing_store = False
        self.failure: tuple[int, str] | None = None
        self.response_override: dict[str, object] | None = None
        self.list_page_cap = 100
        self.race_create = False
        self.race_delete = False
        self._tick = 0
        for method in (responses.GET, responses.POST, responses.PATCH, responses.DELETE):
            http.add_callback(method, re.compile(re.escape(HOST) + r"/.*"), self._handle)

    def client(self) -> DatabricksManagedMemoryClient:
        return DatabricksManagedMemoryClient(
            STORE_ID,
            workspace_client=WorkspaceClient(
                host=HOST, token=SERVICE_TOKEN, auth_type="pat", product="omnigent"
            ),
        )

    def _reply(self, status: int, payload: dict[str, object]) -> tuple[int, dict[str, str], str]:
        return status, {"Content-Type": "application/json"}, json.dumps(payload)

    def _error(self, status: int, code: str) -> tuple[int, dict[str, str], str]:
        return self._reply(status, {"error_code": code, "message": "private-test-credential"})

    def _handle(self, request: requests.PreparedRequest) -> tuple[int, dict[str, str], str]:
        url = urlsplit(request.url or "")
        if url.path == "/.well-known/databricks-config":
            return self._reply(200, {"workspace_id": "1234567890"})
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        body = json.loads(request.body) if request.body else {}
        self.requests.append(
            {
                "method": request.method,
                "path": url.path,
                "query": query,
                "body": body,
                "authorization": request.headers.get("Authorization"),
            }
        )
        if self.failure:
            return self._error(*self.failure)
        if request.headers.get("Authorization") != f"Bearer {SERVICE_TOKEN}":
            return self._error(401, "UNAUTHENTICATED")
        base = f"/api/2.0/agents/{STORE_NAME}"
        if self.missing_store or not url.path.startswith(base):
            return self._error(404, "NOT_FOUND")
        if self.response_override is not None:
            return self._reply(200, self.response_override)
        suffix = unquote(url.path[len(base) :])
        if not suffix:
            return self._reply(200, {"name": STORE_NAME})
        if suffix == "/permissions/self":
            return self._reply(200, {"permission": self.permission})
        if suffix == "/entries" and request.method == "POST":
            entry_id = query["managed_memory_entry_id"]
            if entry_id in self.entries:
                return self._error(409, "ALREADY_EXISTS")
            if any(
                (entry["actor_id"], entry["path"], entry.get("session_id"))
                == (body["actor_id"], body["path"], body.get("session_id"))
                for entry in self.entries.values()
            ):
                return self._error(409, "ALREADY_EXISTS")
            self._tick += 1
            timestamp = datetime.fromtimestamp(1_700_000_000 + self._tick, tz=UTC).isoformat()
            self.entries[entry_id] = {
                **body,
                "name": f"{STORE_NAME}/entries/{entry_id}",
                "create_time": timestamp,
                "update_time": timestamp,
            }
            if self.race_create:
                self.race_create = False
                return self._error(409, "ALREADY_EXISTS")
            return self._reply(200, self.entries[entry_id])
        if suffix == "/entries" and request.method == "GET":
            return self._list(query)
        if suffix == "/entries:search":
            terms = body["query"].lower().split()
            ranked = []
            for entry in self._matching(body):
                score = sum(term in str(entry.get("content", "")).lower() for term in terms)
                if score:
                    ranked.append({"managed_memory_entry": entry, "score": float(score)})
            ranked.sort(key=lambda hit: -hit["score"])
            return self._reply(200, {"results": ranked[: body.get("page_size", 10)]})
        if not suffix.startswith("/entries/"):
            return self._error(404, "NOT_FOUND")
        entry_id = suffix.removeprefix("/entries/")
        if entry_id not in self.entries:
            return self._error(404, "NOT_FOUND")
        if request.method == "PATCH":
            for field in query["update_mask"].split(","):
                if field not in ("content", "description"):
                    return self._error(400, "INVALID_PARAMETER_VALUE")
                self.entries[entry_id][field] = body.get(field)
            self._tick += 1
            self.entries[entry_id]["update_time"] = datetime.fromtimestamp(
                1_700_000_000 + self._tick, tz=UTC
            ).isoformat()
        if request.method == "DELETE":
            del self.entries[entry_id]
            if self.race_delete:
                return self._error(404, "NOT_FOUND")
            return 204, {}, ""
        return self._reply(200, self.entries[entry_id])

    def _matching(self, filters: dict) -> list[dict[str, object]]:
        entries = []
        for entry in self.entries.values():
            if entry["actor_id"] != filters["actor_id"]:
                continue
            prefix = filters.get("path_prefix")
            if prefix and not str(entry["path"]).startswith(prefix):
                continue
            if filters.get("path") and entry["path"] != filters["path"]:
                continue
            entries.append(deepcopy(entry))
        return entries

    def _list(self, query: dict[str, str]) -> tuple[int, dict[str, str], str]:
        identity = [query["actor_id"], query.get("path_prefix"), query.get("path")]
        offset = 0
        if query.get("page_token"):
            try:
                token = json.loads(query["page_token"])
                if token[:3] != identity or type(token[3]) is not int or token[3] < 0:
                    raise ValueError("invalid cursor")
                offset = token[3]
            except (ValueError, TypeError, KeyError, IndexError):
                return self._error(400, "INVALID_PARAMETER_VALUE")
        entries = self._matching(query)
        size = min(int(query.get("page_size", "10")), self.list_page_cap)
        selected = entries[offset : offset + size]
        if query.get("view") == "MANAGED_MEMORY_ENTRY_VIEW_BASIC":
            for entry in selected:
                entry.pop("content", None)
        payload: dict[str, object] = {"managed_memory_entries": selected}
        if offset + size < len(entries):
            payload["next_page_token"] = json.dumps([*identity, offset + size])
        return self._reply(200, payload)


@pytest.fixture()
def managed_memory_api(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[ManagedMemoryAPI]:
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "databrickscfg"))
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        yield ManagedMemoryAPI(http)
