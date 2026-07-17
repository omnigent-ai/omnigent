from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.routes.skills import create_skills_router


class _Response:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _RunnerClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str] | None]] = []

    async def get(
        self,
        path: str,
        *,
        params: dict[str, str] | None,
        timeout: float,
    ) -> _Response:
        del timeout
        self.requests.append((path, params))
        include_other_tools = params == {"include_other_tools": "true"}
        if path.endswith("/skills/catalog"):
            return _Response(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "codex-only:abc",
                            "name": "codex-only",
                            "description": "codex-only description",
                            "origin": "personal",
                            "enabled": True,
                            "available": True,
                            "has_conflict": False,
                            "updated_at": None,
                        }
                    ]
                    if include_other_tools
                    else [],
                    "include_other_tools": include_other_tools,
                    "hidden_count": 0 if include_other_tools else 1,
                },
            )
        if path.endswith("/skills/catalog/codex-only%3Aabc") and include_other_tools:
            return _Response(
                200,
                {
                    "id": "codex-only:abc",
                    "name": "codex-only",
                    "content": "runner-owned content",
                    "provenance": {"original_path": "/runner/home/.codex/skills/codex-only"},
                },
            )
        if path.endswith("/skills/catalog/codex-only%3Aabc/files"):
            return _Response(
                200,
                {
                    "object": "list",
                    "data": [
                        {"path": "SKILL.md", "kind": "file", "size": 12},
                        {"path": "references", "kind": "dir", "size": None},
                    ],
                },
            )
        if path.endswith("/skills/catalog/codex-only%3Aabc/file"):
            # A traversal path is refused by the runner with a 400; the proxy
            # must preserve that status rather than masking it as a 500.
            if (params or {}).get("path", "").startswith(".."):
                return _Response(400, {"detail": "path traversal not allowed"})
            return _Response(
                200,
                {
                    "path": "SKILL.md",
                    "size": 12,
                    "is_text": True,
                    "too_large": False,
                    "text": "runner body",
                },
            )
        return _Response(404, {"detail": "Skill not found"})


class _RunnerRouter:
    def __init__(self, client: _RunnerClient) -> None:
        self.client = client
        self.session_ids: list[str] = []

    def client_for_session_resources(self, session_id: str):  # type: ignore[no-untyped-def]
        self.session_ids.append(session_id)
        return type("Routed", (), {"client": self.client})()


def test_catalog_requires_explicit_session_context() -> None:
    runner_client = _RunnerClient()
    app = FastAPI()
    app.include_router(
        create_skills_router(object(), runner_router=_RunnerRouter(runner_client)),  # type: ignore[arg-type]
        prefix="/v1",
    )
    client = TestClient(app)

    response = client.get("/v1/skills")

    assert response.status_code == 422
    assert runner_client.requests == []


def test_catalog_proxies_runner_context_and_preserves_visibility_override() -> None:
    runner_client = _RunnerClient()
    runner_router = _RunnerRouter(runner_client)
    app = FastAPI()
    app.include_router(
        create_skills_router(object(), runner_router=runner_router),  # type: ignore[arg-type]
        prefix="/v1",
    )
    client = TestClient(app)

    listing = client.get(
        "/v1/skills",
        params={"session_id": "conv/remote", "include_other_tools": "true"},
    )
    assert listing.status_code == 200
    skill_id = listing.json()["data"][0]["id"]
    detail = client.get(
        f"/v1/skills/{skill_id}",
        params={"session_id": "conv/remote", "include_other_tools": "true"},
    )

    assert detail.status_code == 200
    assert detail.json()["content"] == "runner-owned content"
    assert runner_router.session_ids == ["conv/remote", "conv/remote"]
    assert runner_client.requests == [
        (
            "/v1/sessions/conv%2Fremote/skills/catalog",
            {"include_other_tools": "true"},
        ),
        (
            "/v1/sessions/conv%2Fremote/skills/catalog/codex-only%3Aabc",
            {"include_other_tools": "true"},
        ),
    ]


def test_catalog_detail_uses_persisted_visibility_when_override_is_omitted() -> None:
    runner_client = _RunnerClient()
    app = FastAPI()
    app.include_router(
        create_skills_router(object(), runner_router=_RunnerRouter(runner_client)),  # type: ignore[arg-type]
        prefix="/v1",
    )
    client = TestClient(app)

    response = client.get(
        "/v1/skills/codex-only:abc",
        params={"session_id": "conv_remote"},
    )

    assert response.status_code == 404
    assert runner_client.requests == [
        ("/v1/sessions/conv_remote/skills/catalog/codex-only%3Aabc", None)
    ]


def test_skill_files_proxy_lists_tree_and_reads_file() -> None:
    runner_client = _RunnerClient()
    runner_router = _RunnerRouter(runner_client)
    app = FastAPI()
    app.include_router(
        create_skills_router(object(), runner_router=runner_router),  # type: ignore[arg-type]
        prefix="/v1",
    )
    client = TestClient(app)

    tree = client.get(
        "/v1/skills/codex-only:abc/files",
        params={"session_id": "conv_remote", "include_other_tools": "true"},
    )
    assert tree.status_code == 200
    assert {n["path"] for n in tree.json()["data"]} == {"SKILL.md", "references"}

    file_resp = client.get(
        "/v1/skills/codex-only:abc/file",
        params={"session_id": "conv_remote", "path": "SKILL.md", "include_other_tools": "true"},
    )
    assert file_resp.status_code == 200
    assert file_resp.json()["text"] == "runner body"
    # The file read forwards the path param (plus the visibility flag) verbatim.
    assert (
        "/v1/sessions/conv_remote/skills/catalog/codex-only%3Aabc/file",
        {"path": "SKILL.md", "include_other_tools": "true"},
    ) in runner_client.requests


def test_skill_file_proxy_preserves_runner_400_for_traversal() -> None:
    runner_client = _RunnerClient()
    app = FastAPI()
    app.include_router(
        create_skills_router(object(), runner_router=_RunnerRouter(runner_client)),  # type: ignore[arg-type]
        prefix="/v1",
    )
    client = TestClient(app)

    resp = client.get(
        "/v1/skills/codex-only:abc/file",
        params={"session_id": "conv_remote", "path": "../../etc/passwd"},
    )
    # A runner 400 (traversal refusal) must surface as a 400, not a masked 500.
    assert resp.status_code == 400


def test_trust_routes_remain_server_persisted(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(
        create_skills_router(object(), runner_router=_RunnerRouter(_RunnerClient())),  # type: ignore[arg-type]
        prefix="/v1",
    )
    client = TestClient(app)

    assert client.get("/v1/skills/trust").json() == {
        "value": "current",
        "include_other_tools": False,
    }
    assert client.put("/v1/skills/trust", json={"value": "all-host"}).json() == {
        "value": "all-host",
        "include_other_tools": True,
    }


# ── all-agent browse aggregation (_AgentBundleAggregator) ─────────────────────

from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from omnigent.server.routes.skills import _AgentBundleAggregator  # noqa: E402
from omnigent.spec.types import SkillSpec  # noqa: E402


def _bundle_agent(tmp_path: Path, agent_id: str, name: str, skill_name: str):
    """A fake registered agent + AgentCache pair backed by a real bundle dir."""
    sdir = tmp_path / agent_id / "skills" / skill_name
    sdir.mkdir(parents=True)
    (sdir / "SKILL.md").write_text(f"---\nname: {skill_name}\ndescription: {name} skill\n---\nbody\n")
    skill = SkillSpec(name=skill_name, description=f"{name} skill", content="body", skill_dir=sdir)
    spec = SimpleNamespace(
        name=name,
        skills=[skill],
        skills_filter="all",
        skill_trust="all-host",
        executor=SimpleNamespace(harness_kind="claude-sdk"),
    )
    workdir = tmp_path / agent_id
    agent = SimpleNamespace(id=agent_id, name=name, bundle_location=f"{agent_id}/rev1")
    return agent, SimpleNamespace(spec=spec, workdir=workdir)


def _aggregator(agents_and_loaded: dict[str, tuple[Any, Any]]) -> _AgentBundleAggregator:
    agents = [a for a, _ in agents_and_loaded.values()]
    loaded_by_id = {aid: loaded for aid, (_, loaded) in agents_and_loaded.items()}

    store = SimpleNamespace(
        list=lambda limit=200: SimpleNamespace(data=agents),
        get=lambda aid: next((a for a in agents if a.id == aid), None),
    )

    def _load(agent_id: str, loc: str):
        return loaded_by_id[agent_id]

    cache = SimpleNamespace(load=_load)
    return _AgentBundleAggregator(store, cache)


def test_aggregates_all_agents_with_availability(tmp_path: Path) -> None:
    polly = _bundle_agent(tmp_path, "ag_polly", "polly", "cross-review")
    debby = _bundle_agent(tmp_path, "ag_debby", "debby", "debate")
    agg = _aggregator({"ag_polly": polly, "ag_debby": debby})

    # Bound to polly: a runner payload already lists polly's bundled skill.
    payload = {
        "object": "list",
        "data": [
            {
                "id": "cross-review:hash",
                "name": "cross-review",
                "ownership": "agent",
                "agent_name": "polly",
                "display_path": "Included with agent",
            }
        ],
        "include_other_tools": True,
        "hidden_count": 0,
    }
    out = agg.aggregate_into(payload, bound_agent_id="ag_polly")
    rows = out["data"]
    by_name = {r["name"]: r for r in rows}

    # Debby's bundle skill now appears (from a polly session), marked not
    # invokable + required agent.
    assert "debate" in by_name
    assert by_name["debate"]["ownership"] == "agent"
    assert by_name["debate"]["agent_name"] == "debby"
    assert by_name["debate"]["invokable_in_current_session"] is False
    assert by_name["debate"]["required_agent_name"] == "debby"

    # Polly's own row stays invokable and is NOT duplicated.
    assert by_name["cross-review"]["invokable_in_current_session"] is True
    assert by_name["cross-review"]["agent_id"] == "ag_polly"
    assert sum(1 for r in rows if r["name"] == "cross-review") == 1


def test_same_name_skills_across_agents_are_distinct_entries(tmp_path: Path) -> None:
    a = _bundle_agent(tmp_path, "ag_a", "alpha", "review")
    b = _bundle_agent(tmp_path, "ag_b", "beta", "review")
    agg = _aggregator({"ag_a": a, "ag_b": b})

    payload = {"object": "list", "data": [], "include_other_tools": True, "hidden_count": 0}
    out = agg.aggregate_into(payload, bound_agent_id=None)
    review_ids = [r["id"] for r in out["data"] if r["name"] == "review"]
    # Two same-named skills from different agents → two DISTINCT browse ids.
    assert len(review_ids) == 2
    assert len(set(review_ids)) == 2
    assert all(rid.startswith("agent:ag_") for rid in review_ids)


def test_bundle_entries_cached_by_bundle_location(tmp_path: Path) -> None:
    polly = _bundle_agent(tmp_path, "ag_polly", "polly", "cross-review")
    calls = {"n": 0}
    agent, loaded = polly

    def _load(agent_id: str, loc: str):
        calls["n"] += 1
        return loaded

    store = SimpleNamespace(
        list=lambda limit=200: SimpleNamespace(data=[agent]),
        get=lambda aid: agent if aid == agent.id else None,
    )
    agg = _AgentBundleAggregator(store, SimpleNamespace(load=_load))

    base = {"object": "list", "data": [], "include_other_tools": True, "hidden_count": 0}
    agg.aggregate_into(dict(base, data=[]), bound_agent_id=None)
    agg.aggregate_into(dict(base, data=[]), bound_agent_id=None)
    # The bundle is loaded/serialized once and reused (cached by bundle_location).
    assert calls["n"] == 1


def test_aggregated_detail_and_bundle_dir_resolve(tmp_path: Path) -> None:
    debby = _bundle_agent(tmp_path, "ag_debby", "debby", "debate")
    agg = _aggregator({"ag_debby": debby})
    # Discover the aggregated browse id.
    out = agg.aggregate_into(
        {"object": "list", "data": [], "include_other_tools": True, "hidden_count": 0},
        bound_agent_id=None,
    )
    skill_id = next(r["id"] for r in out["data"] if r["name"] == "debate")

    detail = agg.detail_for(skill_id, required_agent_id="ag_debby", agent_name="debby")
    assert detail is not None
    assert detail["invokable_in_current_session"] is False
    assert detail["required_agent_name"] == "debby"
    assert "content" in detail and "provenance" in detail

    bundle_dir = agg.bundle_dir_for(skill_id)
    assert bundle_dir is not None
    assert (Path(bundle_dir) / "SKILL.md").is_file()
