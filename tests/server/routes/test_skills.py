from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.routes.skills import create_skills_router


def _skill(root: Path, name: str) -> None:
    path = root / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} description\n---\n{name} content\n"
    )


def test_catalog_detail_and_trust_routes(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    _skill(home / ".claude" / "skills", "claude-only")
    _skill(home / ".codex" / "skills", "codex-only")
    monkeypatch.setattr("omnigent.server.routes.skills.Path.home", lambda: home)
    monkeypatch.setattr("omnigent.server.routes.skills.Path.cwd", lambda: tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(data))
    app = FastAPI()
    app.include_router(create_skills_router(), prefix="/v1")
    client = TestClient(app)

    default = client.get("/v1/skills")
    assert default.status_code == 200
    assert [item["name"] for item in default.json()["data"]] == ["claude-only"]
    assert default.json()["hidden_count"] == 1

    updated = client.put("/v1/skills/trust", json={"value": "all-host"})
    assert updated.json()["include_other_tools"] is True
    listing = client.get("/v1/skills").json()
    assert {item["name"] for item in listing["data"]} == {"claude-only", "codex-only"}
    detail = client.get(f"/v1/skills/{listing['data'][0]['id']}")
    assert detail.status_code == 200
    assert detail.json()["provenance"]["digest"]
