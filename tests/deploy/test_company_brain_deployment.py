from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]


def _dotenv(path: Path) -> dict[str, str]:
    return {
        key: value
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
        for key, value in [line.split("=", 1)]
    }


def test_server_image_contains_the_qualified_gbrain_runtime() -> None:
    dockerfile = (_ROOT / "deploy/docker/Dockerfile").read_text(encoding="utf-8")

    assert "ARG BUN_VERSION=1.2.23" in dockerfile
    assert "ARG GBRAIN_VERSION=0.46.30.0" in dockerfile
    assert "ARG GBRAIN_COMMIT=872c3d6ae4073eb6e77c661d0a72f30b31c4c999" in dockerfile
    assert "github:garrytan/gbrain#${GBRAIN_COMMIT}" in dockerfile
    assert 'installed="$(gbrain --version)"' in dockerfile
    assert 'expected="gbrain ${GBRAIN_VERSION}"' in dockerfile


def test_compose_isolates_and_shares_only_company_brain_state() -> None:
    compose = yaml.safe_load(
        (_ROOT / "deploy/docker/docker-compose.yaml").read_text(encoding="utf-8")
    )
    services = compose["services"]

    assert services["gbrain-postgres"]["profiles"] == ["company-brain"]
    assert services["gbrain-postgres"]["image"] == (
        "pgvector/pgvector:0.8.1-pg16@"
        "sha256:33198da2828a14c30348d2ccb4750833d5ed9a44c88d840a0e523d7417120337"
    )
    assert services["gbrain-postgres"]["entrypoint"] == ["/bin/sh", "-ec"]
    assert "GBRAIN_POSTGRES_PASSWORD is required" in services["gbrain-postgres"]["command"][0]
    assert services["gbrain-postgres"]["volumes"] == [
        "gbrain-postgres-data:/var/lib/postgresql/data"
    ]
    assert services["gbrain"]["profiles"] == ["company-brain"]
    assert services["gbrain"]["depends_on"]["gbrain-postgres"]["condition"] == "service_healthy"
    assert services["gbrain"]["environment"]["GBRAIN_DATABASE_URL"].endswith(
        "@gbrain-postgres:5432/gbrain"
    )
    assert services["gbrain"]["volumes"] == ["company-brain-data:/data/company-brain"]
    assert services["gbrain"]["ports"] == ["127.0.0.1:${GBRAIN_PORT:-3131}:3131"]
    assert services["gbrain"]["healthcheck"]["test"] == [
        "CMD",
        "sh",
        "/build/integrations/company_brain/scripts/healthcheck.sh",
    ]
    assert (_ROOT / "integrations/company_brain/scripts/healthcheck.sh").is_file()
    assert services["omnigent"]["depends_on"]["gbrain"] == {
        "condition": "service_healthy",
        "required": False,
    }
    assert services["omnigent"]["volumes"] == [
        "artifact-data:/data",
        "company-brain-data:/data/company-brain",
    ]
    omnigent_environment = services["omnigent"]["environment"]
    assert set(omnigent_environment) >= {
        "GBRAIN_DATABASE_URL",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_WORKSPACE_DOMAIN",
        "NOTION_OAUTH_CLIENT_ID",
        "NOTION_OAUTH_CLIENT_SECRET",
        "OMNIGENT_COMPANY_BRAIN_DATA_DIR",
        "OMNIGENT_COMPANY_BRAIN_ENCRYPTION_KEY",
        "OMNIGENT_COMPANY_BRAIN_GBRAIN_STATE_PATH",
        "OMNIGENT_COMPANY_BRAIN_GIT_PUSH",
        "OMNIGENT_COMPANY_BRAIN_MCP_TOKEN",
        "OMNIGENT_COMPANY_BRAIN_MCP_URL",
        "OMNIGENT_COMPANY_BRAIN_OAUTH_STATE_SECRET",
        "OMNIGENT_COMPANY_BRAIN_REPO_PATH",
        "OMNIGENT_COMPANY_BRAIN_REPO_URL",
        "SLACK_OAUTH_CLIENT_ID",
        "SLACK_OAUTH_CLIENT_SECRET",
    }
    assert set(compose["volumes"]) >= {
        "artifact-data",
        "company-brain-data",
        "gbrain-postgres-data",
        "postgres-data",
    }


def test_bootstrap_generates_company_brain_secrets_once(tmp_path: Path) -> None:
    shutil.copy2(_ROOT / "deploy/docker/bootstrap.sh", tmp_path / "bootstrap.sh")
    shutil.copy2(_ROOT / "deploy/docker/.env.example", tmp_path / ".env.example")

    first = subprocess.run(
        ["bash", str(tmp_path / "bootstrap.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr
    assert "GBRAIN_POSTGRES_PASSWORD" not in _dotenv(tmp_path / ".env")

    enabled = subprocess.run(
        ["bash", str(tmp_path / "bootstrap.sh"), "--company-brain"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert enabled.returncode == 0, enabled.stderr
    first_values = _dotenv(tmp_path / ".env")

    expected_lengths = {
        "GBRAIN_POSTGRES_PASSWORD": 32,
        "GBRAIN_ADMIN_BOOTSTRAP_TOKEN": 64,
        "OMNIGENT_COMPANY_BRAIN_ENCRYPTION_KEY": 64,
        "OMNIGENT_COMPANY_BRAIN_OAUTH_STATE_SECRET": 64,
    }
    for key, length in expected_lengths.items():
        assert re.fullmatch(rf"[0-9a-f]{{{length}}}", first_values[key])

    second = subprocess.run(
        ["bash", str(tmp_path / "bootstrap.sh"), "--company-brain"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert second.returncode == 0, second.stderr
    second_values = _dotenv(tmp_path / ".env")
    for key in expected_lengths:
        assert second_values[key] == first_values[key]


def test_bootstrap_rejects_invalid_company_brain_secret_material(tmp_path: Path) -> None:
    shutil.copy2(_ROOT / "deploy/docker/bootstrap.sh", tmp_path / "bootstrap.sh")
    shutil.copy2(_ROOT / "deploy/docker/.env.example", tmp_path / ".env.example")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_openssl = fake_bin / "openssl"
    fake_openssl.write_text("#!/bin/sh\nprintf short\n", encoding="utf-8")
    fake_openssl.chmod(0o755)

    result = subprocess.run(
        ["bash", str(tmp_path / "bootstrap.sh"), "--company-brain"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert result.returncode != 0
    assert "openssl returned invalid material for GBRAIN_POSTGRES_PASSWORD" in result.stderr
