"""Tests for the ``skillpack`` CLI (examples/uc-oss-skillpack).

Covers the two pieces worth pinning without Docker:

1. ``pack_skill`` — SKILL.md frontmatter parsing (delegated to the project
   parser) + a deterministic tar.gz.
2. ``push`` → ``list`` → ``pull`` round-trip through a real
   :class:`UnityCatalogOSSArtifactStore` whose REST volume lookup is mocked
   with :class:`httpx.MockTransport` and whose bytes live on a temp filesystem.

The CLI module lives at a hyphenated path (``examples/uc-oss-skillpack/
skillpack.py``) that isn't a normal importable package, so it's loaded by file
path via importlib.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
import types
from pathlib import Path

import httpx
import pytest

from omnigent.stores.artifact_store.unitycatalog_oss import (
    UnityCatalogOSSArtifactStore,
    UnityCatalogRestClient,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI_PATH = _REPO_ROOT / "examples" / "uc-oss-skillpack" / "skillpack.py"
_VOLUME = "unity.omnigent.skillpacks"


def _load_cli() -> types.ModuleType:
    """Load the skillpack CLI module from its hyphenated file path."""
    spec = importlib.util.spec_from_file_location("skillpack_cli", _CLI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve the module by name
    # (dataclasses looks up cls.__module__ in sys.modules during build).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


skillpack = _load_cli()


def _write_skill(root: Path, name: str, description: str, body: str = "Body.") -> Path:
    """Create a minimal skill directory with a valid SKILL.md."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"
    )
    (skill_dir / "reference.md").write_text("Extra resource.\n")
    return skill_dir


# ── pack_skill ──────────────────────────────────────────────


def test_pack_skill_reads_frontmatter(tmp_path):
    """pack_skill returns the name/description parsed from SKILL.md, so pushed
    packs and the metadata table report what's actually stored."""
    skill_dir = _write_skill(tmp_path, "code-review", "Reviews code changes.")
    packed = skillpack.pack_skill(skill_dir)
    assert packed.spec.name == "code-review"
    assert packed.spec.description == "Reviews code changes."
    assert isinstance(packed.blob, bytes) and len(packed.blob) > 0


def test_pack_skill_archive_contains_files_under_name_prefix(tmp_path):
    """The tar.gz holds the skill's files under a <name>/ prefix, so pull
    reproduces the directory tree."""
    skill_dir = _write_skill(tmp_path, "triage", "Triages inbound requests.")
    packed = skillpack.pack_skill(skill_dir)
    with tarfile.open(fileobj=io.BytesIO(packed.blob), mode="r:gz") as tar:
        names = sorted(tar.getnames())
    assert names == ["triage/SKILL.md", "triage/reference.md"]


def test_pack_skill_is_deterministic(tmp_path, monkeypatch):
    """Packing the same directory twice yields byte-identical output even when
    wall-clock time advances between the two packs. This is meaningful only
    because the gzip layer is written with a fixed mtime=0 header (mode="w:gz"
    stamps a live mtime and would fail this)."""
    skill_dir = _write_skill(tmp_path, "same", "Deterministic pack.")

    # First pack at t=1000, second at t=2000 — a different second, so a live
    # gzip-header mtime would differ between the two blobs.
    monkeypatch.setattr("time.time", lambda: 1000.0)
    blob1 = skillpack.pack_skill(skill_dir).blob
    monkeypatch.setattr("time.time", lambda: 2000.0)
    blob2 = skillpack.pack_skill(skill_dir).blob

    assert blob1 == blob2
    # gzip header stores mtime little-endian in bytes 4..8; assert it's zeroed
    # so the determinism doesn't merely rely on both calls landing in the same
    # second.
    assert blob1[4:8] == b"\x00\x00\x00\x00"


def test_pack_skill_missing_skill_md(tmp_path):
    """A directory without SKILL.md fails loud rather than packing a nameless
    blob."""
    empty = tmp_path / "not-a-skill"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match=r"SKILL\.md"):
        skillpack.pack_skill(empty)


@pytest.mark.parametrize(
    "bad_name",
    ["/escape", "../escape", "a/b", "C:\\evil", "\\\\unc\\share", "..", ""],
)
def test_pack_skill_rejects_unsafe_names(tmp_path, bad_name):
    """A malicious/malformed SKILL.md `name:` is rejected at pack time, before
    it can become a traversing tar member or a corrupt store key. Proves the
    PACKER (not just _safe_extract) refuses unsafe names."""
    # The directory name is safe; only the frontmatter `name:` is hostile.
    skill_dir = tmp_path / "src"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {bad_name!r}\ndescription: hostile.\n---\n\nBody.\n"
    )
    with pytest.raises(ValueError, match="invalid skill name"):
        skillpack.pack_skill(skill_dir)


@pytest.mark.parametrize(
    "bad_name",
    ["/escape", "../escape", "a/b", "C:\\evil", "\\\\unc\\share"],
)
def test_blob_key_rejects_unsafe_names(bad_name):
    """_blob_key validates the name too, so the store key and the tar arcname
    can't diverge on an unsafe name."""
    with pytest.raises(ValueError, match="invalid skill name"):
        skillpack._blob_key(bad_name)


# ── push / list / pull round-trip ───────────────────────────


@pytest.fixture()
def store_factory(tmp_path):
    """Factory returning a store backed by a mocked REST volume + temp FS.

    The volume storage_location is reported as file://{tmp_path}/vol and the
    table-registration POST is accepted.
    """
    storage = (tmp_path / "vol").as_uri()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith(f"/volumes/{_VOLUME}"):
            return httpx.Response(
                200,
                json={
                    "catalog_name": "unity",
                    "schema_name": "omnigent",
                    "name": "skillpacks",
                    "volume_id": "vol-1",
                    "volume_type": "EXTERNAL",
                    "storage_location": storage,
                    "full_name": _VOLUME,
                },
            )
        if request.method == "POST" and path.endswith("/tables"):
            return httpx.Response(200, json={"name": "skills"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    def _factory() -> UnityCatalogOSSArtifactStore:
        client = httpx.Client(
            base_url="http://uc.test/api/2.1/unity-catalog",
            transport=httpx.MockTransport(handler),
        )
        rest = UnityCatalogRestClient(uri="http://uc.test", client=client)
        return UnityCatalogOSSArtifactStore(storage_location=_VOLUME, client=rest)

    return _factory


def test_push_stores_blob_and_manifest(tmp_path, store_factory):
    """A push writes the pack blob AND a manifest row for it, so list/pull can
    find the pack afterward."""
    skill_dir = _write_skill(tmp_path / "src", "alpha", "First skill.")
    store = store_factory()

    packed = skillpack.pack_skill(skill_dir)
    store.put(skillpack._blob_key("alpha"), packed.blob)
    manifest = skillpack._load_manifest(store)
    manifest["alpha"] = skillpack.SkillPackRecord(
        name="alpha",
        description="First skill.",
        blob_key=skillpack._blob_key("alpha"),
        version=1,
        updated_at=1,
        source=str(skill_dir),
    )
    skillpack._save_manifest(store, manifest)

    assert store.get(skillpack._blob_key("alpha")) == packed.blob
    reloaded = skillpack._load_manifest(store)
    assert reloaded["alpha"].description == "First skill."
    assert reloaded["alpha"].blob_key == "skills/alpha/alpha.tar.gz"


def test_push_list_pull_round_trip_via_cli(tmp_path, monkeypatch, store_factory, capsys):
    """End-to-end through the real cmd_push/cmd_list/cmd_pull handlers, with
    _make_store pinned to the mocked store. Guards the CLI arg wiring and the
    manifest version/upsert logic."""
    skill_dir = _write_skill(tmp_path / "src", "beta", "Second skill.")
    store = store_factory()
    monkeypatch.setattr(skillpack, "_make_store", lambda args: store)

    parser = skillpack.build_parser()

    rc = skillpack.cmd_push(parser.parse_args(["push", str(skill_dir)]))
    assert rc == 0
    assert "beta v1" in capsys.readouterr().out

    # push again → version increments to 2 (upsert, not duplicate)
    rc = skillpack.cmd_push(parser.parse_args(["push", str(skill_dir)]))
    assert rc == 0
    assert "beta v2" in capsys.readouterr().out
    manifest = skillpack._load_manifest(store)
    assert list(manifest) == ["beta"]
    assert manifest["beta"].version == 2

    rc = skillpack.cmd_list(parser.parse_args(["list"]))
    assert rc == 0
    list_out = capsys.readouterr().out
    assert "beta" in list_out and "Second skill." in list_out

    dest = tmp_path / "pulled"
    rc = skillpack.cmd_pull(parser.parse_args(["pull", "beta", str(dest)]))
    assert rc == 0
    pulled = dest / "beta" / "SKILL.md"
    assert pulled.read_text() == (skill_dir / "SKILL.md").read_text()


def test_push_survives_table_registration_failure(tmp_path, monkeypatch, capsys):
    """A non-409 error from the /tables endpoint (a real UC OSS server rejects
    the EXTERNAL JSON table with 400) must NOT abort push: the blob + manifest
    are the source of truth and are already written, and table registration is
    inspectability-only. Guards demo.sh against a non-atomic push abort."""
    storage = (tmp_path / "vol").as_uri()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith(f"/volumes/{_VOLUME}"):
            return httpx.Response(
                200,
                json={
                    "catalog_name": "unity",
                    "schema_name": "omnigent",
                    "name": "skillpacks",
                    "volume_id": "vol-1",
                    "volume_type": "EXTERNAL",
                    "storage_location": storage,
                    "full_name": _VOLUME,
                },
            )
        if request.method == "POST" and path.endswith("/tables"):
            # What real UC OSS returns: storage_location not under a registered
            # external location.
            return httpx.Response(400, json={"error_code": "INVALID_PARAMETER_VALUE"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.Client(
        base_url="http://uc.test/api/2.1/unity-catalog",
        transport=httpx.MockTransport(handler),
    )
    rest = UnityCatalogRestClient(uri="http://uc.test", client=client)
    store = UnityCatalogOSSArtifactStore(storage_location=_VOLUME, client=rest)
    monkeypatch.setattr(skillpack, "_make_store", lambda args: store)

    skill_dir = _write_skill(tmp_path / "src", "gamma", "Registration-failure skill.")
    parser = skillpack.build_parser()

    # push must succeed (exit 0) despite the 400 from /tables ...
    rc = skillpack.cmd_push(parser.parse_args(["push", str(skill_dir)]))
    assert rc == 0
    captured = capsys.readouterr()
    assert "gamma v1" in captured.out
    # ... and it must warn (best-effort) rather than raise.
    assert "table registration skipped" in captured.err
    assert "400" in captured.err

    # The blob + manifest are still stored, so the round-trip is intact.
    assert store.get(skillpack._blob_key("gamma")) == skillpack.pack_skill(skill_dir).blob
    assert skillpack._load_manifest(store)["gamma"].blob_key == "skills/gamma/gamma.tar.gz"


def test_pull_unknown_name_errors(tmp_path, monkeypatch, store_factory, capsys):
    """pull of an unknown pack name returns exit code 1 with a clear message."""
    store = store_factory()
    monkeypatch.setattr(skillpack, "_make_store", lambda args: store)
    parser = skillpack.build_parser()
    rc = skillpack.cmd_pull(parser.parse_args(["pull", "ghost", str(tmp_path / "d")]))
    assert rc == 1
    assert "no skill pack named 'ghost'" in capsys.readouterr().err


def test_safe_extract_rejects_traversal(tmp_path):
    """_safe_extract refuses archive members that escape the dest, blocking
    path-traversal via a malicious pack."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"evil"
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    dest = tmp_path / "out"
    dest.mkdir()
    with (
        tarfile.open(fileobj=buf, mode="r:gz") as tar,
        pytest.raises(ValueError, match="unsafe path"),
    ):
        skillpack._safe_extract(tar, dest)
