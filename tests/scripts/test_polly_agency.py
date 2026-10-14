"""Exercise pinned imports and safe updates using disposable Git repositories."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "examples/polly/skills/agency-router"
SPEC = importlib.util.spec_from_file_location("polly_agency", ROOT / "scripts/polly_agency.py")
assert SPEC and SPEC.loader
agency = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agency)


def init_repo(path: Path) -> Path:
    path.mkdir()
    agency.git(path, "init", "-b", "main")
    agency.git(path, "config", "user.name", "Fixture")
    agency.git(path, "config", "user.email", "fixture@example.test")
    return path


def commit(repo: Path, message: str = "fixture") -> str:
    agency.git(repo, "add", ".")
    agency.git(repo, "commit", "-m", message)
    return agency.git(repo, "rev-parse", "HEAD")


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = init_repo(tmp_path / "agency")
    (repo / "engineering").mkdir()
    for _, name, slug in agency.ROLES.values():
        (repo / f"engineering/engineering-{slug}.md").write_text(
            f"---\nname: {name}\ndescription: Domain advice\n---\n\n# Body for {name}\n"
        )
    (repo / "LICENSE").write_text("MIT test license\n")
    commit(repo)
    return repo


def test_deterministic_portable_build_and_rollback(source_repo: Path, tmp_path: Path) -> None:
    sha = agency.git(source_repo, "rev-parse", "HEAD")
    manifest = agency.pin(source_repo, sha)
    active, copy_bundle = tmp_path / "active", tmp_path / "copy"
    agency.build(source_repo, manifest, active)
    agency.build(source_repo, manifest, copy_bundle)
    for file in active.rglob("*"):
        if file.is_file():
            assert file.read_bytes() == (copy_bundle / file.relative_to(active)).read_bytes()
    # Working-tree edits cannot change a pinned Git-object import.
    source = source_repo / manifest["personas"][0]["path"]
    source.write_text(source.read_text() + "\nChanged guidance\n")
    assert agency.pin(source_repo, sha) == manifest
    new_sha = commit(source_repo, "new guidance")
    candidate = tmp_path / "candidate"
    agency.build(source_repo, agency.pin(source_repo, new_sha), candidate)
    backup = tmp_path / "previous"
    agency.promote(candidate, active, backup)
    assert agency.validate(active)["upstream_sha"] == new_sha
    # Rollback is the same explicit promotion, preserving the rejected release.
    agency.promote(backup, active, tmp_path / "rejected")
    assert agency.validate(active) == manifest
    shutil.rmtree(source_repo)
    assert agency.validate(active) == manifest
    assert json.loads((active / "catalog.json").read_bytes())[0]["alias"] == "Backend Developer"


@pytest.mark.parametrize(
    "header",
    [
        "name: X\nname: Y\ndescription: text",
        "name: []\ndescription: text",
        "name: X",
        "name: X\ndescription: null",
        "name: X\ndescription: |\n  multi\n  line",
        "name: X\ndescription: text\nnew_field: surprise",
        "[name, description]",
        "name: X\ndescription: !!python/object:bad {}",
    ],
)
def test_bad_metadata(header: str) -> None:
    with pytest.raises((ValueError, agency.yaml.YAMLError)):
        agency.metadata(f"---\n{header}\n---\nBody\n".encode())


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../../b", "a\\b", "a//b", "./a"])
def test_path_traversal(path: str) -> None:
    with pytest.raises(ValueError, match="Unsafe path"):
        agency.safe_path(path)


@pytest.mark.parametrize("fault", ["duplicate", "missing", "path", "alias", "hash", "version"])
def test_bad_manifest(source_repo: Path, tmp_path: Path, fault: str) -> None:
    manifest = agency.pin(source_repo, agency.git(source_repo, "rev-parse", "HEAD"))
    if fault == "duplicate":
        manifest["personas"].append(copy.deepcopy(manifest["personas"][0]))
    elif fault == "missing":
        manifest["personas"].pop()
    elif fault == "path":
        manifest["personas"][0]["path"] = "../escape.md"
    elif fault == "alias":
        manifest["personas"][0]["alias"] = "Invented Persona"
    elif fault == "hash":
        manifest["personas"][0]["sha256"] = "0" * 64
    else:
        manifest["converter_version"] = 2
    with pytest.raises(ValueError):
        agency.build(source_repo, manifest, tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("fault", ["deleted", "renamed", "symlink", "title"])
def test_changed_upstream(source_repo: Path, fault: str) -> None:
    source = source_repo / "engineering/engineering-code-reviewer.md"
    if fault == "title":
        source.write_text(source.read_text().replace("name: Code Reviewer", "name: New Reviewer"))
    elif fault == "renamed":
        source.rename(source.with_name("new-name.md"))
    else:
        source.unlink()
        if fault == "symlink":
            source.symlink_to("../LICENSE")
    sha = commit(source_repo)
    with pytest.raises(ValueError, match=r"renamed|non-regular"):
        agency.pin(source_repo, sha)


@pytest.mark.parametrize("fault", ["body", "catalog", "license", "extra", "symlink"])
def test_bundle_integrity(tmp_path: Path, fault: str) -> None:
    bundle = tmp_path / "bundle"
    shutil.copytree(SKILL / "references/pinned", bundle)
    file = bundle / "personas/code-reviewer.md"
    if fault == "body":
        file.write_text(file.read_text() + "\nInjected advice\n")
    elif fault == "catalog":
        (bundle / "catalog.json").write_text("[]\n")
    elif fault == "license":
        (bundle / "LICENSE").write_text("Changed\n")
    elif fault == "extra":
        (bundle / "user-notes").write_text("keep me")
    else:
        file.unlink()
        file.symlink_to(SKILL / "references/pinned/personas/code-reviewer.md")
    with pytest.raises(ValueError):
        agency.validate(bundle)


def test_build_and_promotion_preserve_existing_files(source_repo: Path, tmp_path: Path) -> None:
    manifest = agency.pin(source_repo, agency.git(source_repo, "rev-parse", "HEAD"))
    active, candidate, backup = (tmp_path / n for n in ("active", "candidate", "backup"))
    agency.build(source_repo, manifest, active)
    with pytest.raises(FileExistsError):
        agency.build(source_repo, manifest, active)
    agency.build(source_repo, manifest, candidate)
    backup.mkdir()
    (backup / "notes").write_text("keep")
    with pytest.raises(ValueError, match="Backup already exists"):
        agency.promote(candidate, active, backup)
    (active / "notes").write_text("user file")
    with pytest.raises(ValueError, match="Unexpected"):
        agency.promote(candidate, active, tmp_path / "other")
    assert (active / "notes").read_text() == "user file"
    assert (backup / "notes").read_text() == "keep"


@pytest.fixture
def tracking_repos(tmp_path: Path) -> tuple[Path, Path]:
    remote = init_repo(tmp_path / "remote")
    (remote / "tracked").write_text("original\n")
    commit(remote)
    local = tmp_path / "local"
    agency.git(tmp_path, "clone", str(remote), str(local))
    agency.git(local, "config", "user.name", "Fixture")
    agency.git(local, "config", "user.email", "fixture@example.test")
    (remote / "tracked").write_text("updated\n")
    commit(remote)
    return remote, local


def test_check_then_explicit_update_independent_repos(tracking_repos: tuple[Path, Path]) -> None:
    remote, local = tracking_repos
    head = agency.git(local, "rev-parse", "HEAD")
    check = agency.repo_update(local)
    assert check["behind"] == 1 and not check["updated"]
    assert agency.git(local, "rev-parse", "HEAD") == head
    assert (local / "tracked").read_text() == "original\n"
    assert agency.repo_update(local, update=True)["updated"]
    assert (local / "tracked").read_text() == "updated\n"
    assert agency.git(remote, "status", "--porcelain") == ""
    assert not agency.repo_update(local, update=True)["updated"]


@pytest.mark.parametrize(
    "fault", ["tracked", "untracked", "diverged", "ahead", "detached", "no-tracking", "ignored"]
)
def test_refuse_unsafe_update(tracking_repos: tuple[Path, Path], fault: str) -> None:
    remote, local = tracking_repos
    if fault == "tracked":
        (local / "tracked").write_text("user change")
    elif fault == "untracked":
        (local / "notes").write_text("user notes")
        assert "notes" in agency.repo_update(local)["status"]
    elif fault in ("diverged", "ahead"):
        if fault == "ahead":
            agency.repo_update(local, update=True)
        (local / "local-commit").write_text("user commit")
        commit(local)
    elif fault == "detached":
        agency.git(local, "checkout", "--detach")
    elif fault == "no-tracking":
        agency.git(local, "branch", "--unset-upstream")
    else:
        (local / ".git/info/exclude").write_text("notes\n")
        (local / "notes").write_text("ignored user file")
        (remote / "notes").write_text("incoming tracked file")
        commit(remote)
    head = agency.git(local, "rev-parse", "HEAD")
    with pytest.raises(ValueError):
        agency.repo_update(local, update=True)
    assert agency.git(local, "rev-parse", "HEAD") == head
    if fault in ("ignored", "untracked"):
        assert "user" in (local / "notes").read_text()


def test_cli_reports_errors_without_overwriting(tmp_path: Path) -> None:
    output = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/polly_agency.py"),
            "validate",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert output.returncode == 1
    assert "Error:" in output.stderr and "Traceback" not in output.stderr


def test_portable_skill_catalog_then_selected_resource(tmp_path: Path) -> None:
    from omnigent.spec.parser import parse
    from omnigent.tools.builtins.read_skill_file import ReadSkillFileTool

    # The copied Polly bundle has no source checkout or installation-time dependency.
    bundle = tmp_path / "polly"
    shutil.copytree(ROOT / "examples/polly", bundle)
    spec = parse(bundle)
    skill = next(s for s in spec.skills if s.name == "agency-router")
    assert len(spec.sub_agents) == 7
    assert "# Backend Architect Agent Personality" not in skill.content
    reader = ReadSkillFileTool([skill])
    # This tool does not consume its context; exercise the real resource read boundary.
    catalog = json.loads(
        reader.invoke(
            json.dumps(
                {
                    "skill_name": skill.name,
                    "path": "references/pinned/catalog.json",
                }
            ),
            None,
        )
    )
    selected = next(row for row in catalog if row["alias"] == "Mobile App Developer")
    assert selected["name"] == "Mobile App Builder"
    content = reader.invoke(
        json.dumps(
            {
                "skill_name": skill.name,
                "path": "references/pinned/" + selected["resource"],
            }
        ),
        None,
    )
    assert content == (SKILL / "references/pinned/personas/mobile-app-developer.md").read_text()
    assert "Error:" in reader.invoke(
        json.dumps(
            {
                "skill_name": skill.name,
                "path": "../outside",
            }
        ),
        None,
    )
