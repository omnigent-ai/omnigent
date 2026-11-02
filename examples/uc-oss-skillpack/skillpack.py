#!/usr/bin/env python3
"""``skillpack`` — store Omnigent skill/knowledge packs in a UC OSS volume.

A small standalone CLI for the Unity Catalog OSS store-only POC. It is
intentionally NOT wired into the main ``omni`` CLI — run it directly::

    python examples/uc-oss-skillpack/skillpack.py push ~/.claude/skills/foo
    python examples/uc-oss-skillpack/skillpack.py list

Subcommands
-----------
- ``pack <skill_dir>``  — tar.gz a skill directory and print the name /
  description parsed from its ``SKILL.md`` frontmatter (no server needed).
- ``push <skill_dir>``  — pack, store the blob in the UC OSS volume, and
  upsert a metadata row (name, description, version, blob key, updated_at)
  into the metadata manifest for the ``unity.omnigent.skills`` table.
- ``list``              — read the metadata manifest and print each pack's
  name / description / updated time.
- ``pull <name> [dest]``— fetch a pack's blob and extract it to *dest*.

Metadata storage — a POC honesty note
--------------------------------------
Managed Databricks can INSERT rows into a UC table over its SQL API.
**UC OSS has no row-level DML over REST** — its REST surface is metadata-only
(create/list/get catalogs, schemas, volumes, tables). So this CLI keeps the
skill metadata "rows" as a JSON manifest blob inside the same volume (key
``_manifest/skills.json``) and *registers* the UC table
``unity.omnigent.skills`` as an EXTERNAL table pointing at that manifest
location, so the three-level name exists and is inspectable via the REST API
(``GET /tables/unity.omnigent.skills``). ``list`` reads the manifest. This is
a deliberate store-only shortcut, not a production metastore design.

POC / not for production.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import sys
import tarfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from omnigent.spec.parser import _parse_skill
from omnigent.spec.types import SkillSpec
from omnigent.stores.artifact_store.unitycatalog_oss import (
    UnityCatalogOSSArtifactStore,
    UnityCatalogRestClient,
    _parse_volume_full_name,
)

# Key of the JSON manifest that holds the skill metadata "rows" inside the
# volume. See the module docstring for why rows live in a blob.
_MANIFEST_KEY = "_manifest/skills.json"
# Default three-level names for the POC. Overridable via UC_OSS_VOLUME /
# --volume and --table.
_DEFAULT_VOLUME = "unity.omnigent.skillpacks"
_DEFAULT_TABLE = "unity.omnigent.skills"


@dataclass
class SkillPackRecord:
    """
    One metadata row for a stored skill/knowledge pack.

    :param name: The skill's ``name`` from its ``SKILL.md`` frontmatter, e.g.
        ``"code-review"``.
    :param description: The skill's ``description`` from frontmatter.
    :param blob_key: The artifact-store key of the pack's tar.gz, e.g.
        ``"skills/code-review/code-review.tar.gz"``.
    :param version: Monotonic push counter for this name (1 on first push,
        incremented on each re-push), e.g. ``2``.
    :param updated_at: Epoch-millisecond timestamp of the last push.
    :param source: Absolute path of the skill directory that was packed, e.g.
        ``"/Users/denny.lee/.claude/skills/code-review"``. Recorded for
        provenance so a reviewer can see where the pack came from.
    """

    name: str
    description: str
    blob_key: str
    version: int
    updated_at: int
    source: str


@dataclass
class PackedSkill:
    """
    The result of packing a skill directory.

    :param spec: The parsed :class:`SkillSpec` (name, description, etc.) read
        from the directory's ``SKILL.md``.
    :param blob: The gzip-compressed tar bytes of the skill directory.
    """

    spec: SkillSpec
    blob: bytes


def _validate_skill_name(name: str) -> str:
    """
    Validate a skill's ``name`` before it becomes path material.

    A skill's ``name`` (from its ``SKILL.md`` frontmatter) is used both as a
    store-key path component (:func:`_blob_key`) and as the tar member prefix
    (the arcname in :func:`pack_skill`). A malformed/malicious name like
    ``/escape``, ``../escape``, ``a/b``, ``C:\\evil``, or ``\\\\unc\\share``
    could otherwise emit an absolute/traversing tar member or a corrupt store
    key (e.g. ``skills//escape//escape.tar.gz``) that stores but won't pull.
    Require a single safe path component: reject empties, ``.``/``..``, forward
    and back slashes, and Windows drive/UNC forms.

    :param name: The ``name`` field parsed from ``SKILL.md``.
    :returns: *name* unchanged when valid (for convenient inline use).
    :raises ValueError: If *name* is not a single safe path component.
    """
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or PurePosixPath(name).is_absolute()
        or PureWindowsPath(name).is_absolute()
        or PureWindowsPath(name).drive
    ):
        raise ValueError(
            f"invalid skill name {name!r}: must be a single path component "
            "(no '/', '\\\\', '..', absolute paths, or Windows drive/UNC forms)"
        )
    return name


def _blob_key(name: str) -> str:
    """
    Compute the artifact-store key for a pack's tar.gz blob.

    :param name: The skill name, e.g. ``"code-review"``. Validated as a single
        safe path component via :func:`_validate_skill_name`.
    :returns: The forward-slash key, e.g.
        ``"skills/code-review/code-review.tar.gz"``.
    :raises ValueError: If *name* is not a single safe path component.
    """
    _validate_skill_name(name)
    return f"skills/{name}/{name}.tar.gz"


def pack_skill(skill_dir: Path) -> PackedSkill:
    """
    Read a skill's frontmatter and produce a byte-stable tar.gz.

    Reuses the project's ``SKILL.md`` frontmatter parser
    (:func:`omnigent.spec.parser._parse_skill`) rather than duplicating the
    YAML-frontmatter logic. The skill ``name`` is validated as a single safe
    path component (:func:`_validate_skill_name`) before it becomes the tar
    member prefix, so a malicious ``name:`` can't emit a traversing arcname.

    The archive is reproducible across time: the gzip layer is written with a
    fixed ``mtime=0`` header (via :class:`gzip.GzipFile`), entries are sorted,
    and each member's mtime / uid / gid / uname / gname are normalized — so
    packing the same directory twice yields byte-identical output regardless of
    wall-clock time.

    :param skill_dir: Path to the skill directory containing a ``SKILL.md``,
        e.g. ``Path("~/.claude/skills/code-review")``.
    :returns: A :class:`PackedSkill` with the parsed spec and the
        gzip-compressed tar bytes of the directory.
    :raises FileNotFoundError: If *skill_dir* is not a directory or has no
        ``SKILL.md``.
    :raises ValueError: If the parsed skill ``name`` is not a single safe path
        component.
    """
    skill_dir = skill_dir.expanduser()
    if not skill_dir.is_dir():
        raise FileNotFoundError(f"not a directory: {skill_dir}")
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise FileNotFoundError(f"no SKILL.md in {skill_dir}")

    spec = _parse_skill(skill_md)
    name = _validate_skill_name(spec.name)

    buf = io.BytesIO()
    # gzip header mtime=0 + sorted entries + normalized member metadata →
    # byte-stable archive across wall-clock time (mode="w:gz" would stamp a
    # live mtime into the gzip header, breaking determinism).
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file():
                continue
            arcname = f"{name}/{path.relative_to(skill_dir).as_posix()}"
            info = tar.gettarinfo(str(path), arcname=arcname)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as fh:
                tar.addfile(info, fh)
    return PackedSkill(spec=spec, blob=buf.getvalue())


def _load_manifest(store: UnityCatalogOSSArtifactStore) -> dict[str, SkillPackRecord]:
    """
    Load the skill metadata manifest from the volume.

    :param store: The UC OSS artifact store backing the volume.
    :returns: Mapping of skill name → :class:`SkillPackRecord`. Empty when no
        manifest has been written yet.
    """
    try:
        raw = store.get(_MANIFEST_KEY)
    except KeyError:
        return {}
    rows = json.loads(raw.decode("utf-8"))
    return {r["name"]: SkillPackRecord(**r) for r in rows}


def _save_manifest(
    store: UnityCatalogOSSArtifactStore, manifest: dict[str, SkillPackRecord]
) -> None:
    """
    Persist the skill metadata manifest to the volume.

    :param store: The UC OSS artifact store backing the volume.
    :param manifest: Mapping of skill name → :class:`SkillPackRecord`.
    """
    rows = [asdict(manifest[name]) for name in sorted(manifest)]
    store.put(_MANIFEST_KEY, json.dumps(rows, indent=2).encode("utf-8"))


def _ensure_table_registered(
    client: UnityCatalogRestClient,
    table: str,
    storage_location: str,
) -> None:
    """
    Best-effort register the metadata table in UC.

    Creates ``unity.omnigent.skills`` as an EXTERNAL table pointing at the
    manifest's storage location so the three-level name exists and is
    inspectable over REST. Row data itself lives in the manifest blob (see the
    module docstring) — this is registration only.

    Registration is **best-effort**: the blob + manifest are the source of
    truth and were already written before this runs. A real UC OSS server
    rejects an EXTERNAL table whose ``storage_location`` isn't under a
    registered external location (HTTP 400), so on any non-2xx (other than a
    409, treated as idempotent success) this logs a concise note to stderr and
    returns rather than raising — a failed table registration must not abort an
    otherwise-successful push. The round-trip (push → list → pull) reads the
    manifest, not this table, so it is unaffected.

    :param client: The UC REST client.
    :param table: Three-level table name, e.g. ``"unity.omnigent.skills"``.
    :param storage_location: The manifest's backing ``file://`` location.
    """
    import httpx

    catalog, schema, name = _parse_volume_full_name(table)
    columns = [
        {
            "name": "name",
            "type_text": "string",
            "type_name": "STRING",
            "position": 0,
            "nullable": False,
        },
        {
            "name": "description",
            "type_text": "string",
            "type_name": "STRING",
            "position": 1,
            "nullable": True,
        },
        {
            "name": "blob_key",
            "type_text": "string",
            "type_name": "STRING",
            "position": 2,
            "nullable": False,
        },
        {
            "name": "version",
            "type_text": "int",
            "type_name": "INT",
            "position": 3,
            "nullable": False,
        },
        {
            "name": "updated_at",
            "type_text": "long",
            "type_name": "LONG",
            "position": 4,
            "nullable": False,
        },
    ]
    body: dict[str, object] = {
        "name": name,
        "catalog_name": catalog,
        "schema_name": schema,
        "table_type": "EXTERNAL",
        "data_source_format": "JSON",
        "columns": columns,
        "storage_location": storage_location,
        "comment": "Omnigent skill/knowledge pack registry (POC, store-only).",
    }
    resp = client._client.post("/tables", json=body)
    if resp.is_success or resp.status_code == httpx.codes.CONFLICT:
        return
    # Non-2xx (e.g. 400 when storage_location isn't under a registered external
    # location): keep the push successful — table registration is inspectability
    # only. Surface a one-line note without the full body.
    reason = resp.reason_phrase or "error"
    print(
        f"note: skill metadata table registration skipped: {resp.status_code} "
        f"{reason}; blob+manifest stored, round-trip unaffected",
        file=sys.stderr,
    )


def _make_store(args: argparse.Namespace) -> UnityCatalogOSSArtifactStore:
    """
    Build a UC OSS artifact store from parsed CLI args / env vars.

    :param args: Parsed argparse namespace carrying ``volume``, ``uri``,
        ``token``, and ``local_root`` (each may be ``None`` to fall back to the
        corresponding ``UC_OSS_*`` env var).
    :returns: A configured :class:`UnityCatalogOSSArtifactStore`.
    """
    # Precedence: --volume flag → UC_OSS_VOLUME env → literal default.
    volume = args.volume or os.environ.get("UC_OSS_VOLUME") or _DEFAULT_VOLUME
    # Keep the namespace in sync so command handlers print the real volume.
    args.volume = volume
    return UnityCatalogOSSArtifactStore(
        storage_location=volume,
        uri=args.uri,
        token=args.token,
        local_root=args.local_root,
    )


def cmd_pack(args: argparse.Namespace) -> int:
    """
    Handle ``skillpack pack`` — tar.gz a skill dir and show metadata.

    :param args: Parsed args carrying ``skill_dir``.
    :returns: Process exit code (0 on success).
    """
    packed = pack_skill(Path(args.skill_dir))
    print(f"name:        {packed.spec.name}")
    print(f"description: {packed.spec.description}")
    print(f"blob_key:    {_blob_key(packed.spec.name)}")
    print(f"size:        {len(packed.blob)} bytes (tar.gz)")
    if args.out:
        Path(args.out).write_bytes(packed.blob)
        print(f"written:     {args.out}")
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    """
    Handle ``skillpack push`` — store a pack blob + upsert metadata.

    :param args: Parsed args carrying ``skill_dir`` (+ store config).
    :returns: Process exit code (0 on success).
    """
    packed = pack_skill(Path(args.skill_dir))
    spec = packed.spec
    store = _make_store(args)
    key = _blob_key(spec.name)
    store.put(key, packed.blob)

    manifest = _load_manifest(store)
    prev = manifest.get(spec.name)
    record = SkillPackRecord(
        name=spec.name,
        description=spec.description,
        blob_key=key,
        version=(prev.version + 1) if prev else 1,
        updated_at=int(time.time() * 1000),
        source=str(Path(args.skill_dir).expanduser().resolve()),
    )
    manifest[spec.name] = record
    _save_manifest(store, manifest)

    # Register the UC metadata table (idempotent). The manifest's backing
    # file:// location is the store's resolved storage dir + manifest key.
    manifest_location = (store._resolve_storage_dir() / _MANIFEST_KEY).as_uri()
    _ensure_table_registered(store._client, args.table, manifest_location)

    verb = "updated" if prev else "stored"
    print(f"{verb} {spec.name} v{record.version} → {args.volume} ({len(packed.blob)} bytes)")
    print(f"  blob_key: {key}")
    print(f"  table:    {args.table}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """
    Handle ``skillpack list`` — print the metadata manifest.

    :param args: Parsed args (store config).
    :returns: Process exit code (0 on success).
    """
    store = _make_store(args)
    manifest = _load_manifest(store)
    if not manifest:
        print(f"(no skill packs in {args.volume})")
        return 0
    print(f"{'NAME':<28} {'VER':>3}  {'UPDATED':<20} DESCRIPTION")
    for name in sorted(manifest):
        r = manifest[name]
        updated = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.updated_at / 1000))
        desc = r.description if len(r.description) <= 60 else r.description[:57] + "..."
        print(f"{r.name:<28} {r.version:>3}  {updated:<20} {desc}")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    """
    Handle ``skillpack pull`` — fetch a pack blob and extract it.

    :param args: Parsed args carrying ``name`` and optional ``dest``.
    :returns: Process exit code (0 on success, 1 if the pack is absent).
    """
    store = _make_store(args)
    manifest = _load_manifest(store)
    record = manifest.get(args.name)
    if record is None:
        print(f"error: no skill pack named {args.name!r} in {args.volume}", file=sys.stderr)
        return 1
    blob = store.get(record.blob_key)
    dest = Path(args.dest or f"./{args.name}").expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        _safe_extract(tar, dest)
    print(f"pulled {args.name} v{record.version} → {dest}")
    return 0


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """
    Extract *tar* into *dest*, rejecting entries that escape *dest*.

    Guards against path-traversal in archive member names (``..`` or absolute
    paths) so a malicious pack cannot write outside the extraction directory.

    :param tar: An open :class:`tarfile.TarFile` in read mode.
    :param dest: The destination directory (already created).
    :raises ValueError: If any member would extract outside *dest*.
    """
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if not target.is_relative_to(dest_resolved):
            raise ValueError(f"unsafe path in archive: {member.name!r}")
    # filter="data" applies the stdlib safe-extraction filter (strips setuid
    # bits, rejects absolute/traversal paths) on top of the containment check.
    tar.extractall(dest, filter="data")


def build_parser() -> argparse.ArgumentParser:
    """
    Build the ``skillpack`` argument parser.

    :returns: The configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="skillpack",
        description="Store Omnigent skill/knowledge packs in a UC OSS volume (POC).",
    )

    def add_store_args(sp: argparse.ArgumentParser) -> None:
        """Attach shared store-config flags to a subparser."""
        sp.add_argument(
            "--volume",
            default=None,
            help=f"three-level volume name (env UC_OSS_VOLUME; default {_DEFAULT_VOLUME})",
        )
        sp.add_argument(
            "--table",
            default=_DEFAULT_TABLE,
            help=f"three-level metadata table name (default {_DEFAULT_TABLE})",
        )
        sp.add_argument(
            "--uri",
            default=None,
            help="UC OSS server URI (env UC_OSS_URI; default http://localhost:8080)",
        )
        # Prefer the env var: a --token on the command line can leak via shell
        # history / the process list. Local UC OSS needs no token at all.
        sp.add_argument(
            "--token",
            default=None,
            help="bearer token (prefer env UC_OSS_TOKEN to avoid shell/process leaks; "
            "local UC OSS needs none)",
        )
        sp.add_argument(
            "--local-root",
            dest="local_root",
            default=None,
            help="host path to the volume storage (env UC_OSS_VOLUME_LOCAL_PATH)",
        )

    sub = parser.add_subparsers(dest="command", required=True)

    p_pack = sub.add_parser("pack", help="tar.gz a skill dir and show its metadata")
    p_pack.add_argument("skill_dir", help="path to a skill directory containing SKILL.md")
    p_pack.add_argument("--out", default=None, help="also write the tar.gz to this path")
    p_pack.set_defaults(func=cmd_pack)

    p_push = sub.add_parser("push", help="pack + store a skill pack and upsert its metadata")
    p_push.add_argument("skill_dir", help="path to a skill directory containing SKILL.md")
    add_store_args(p_push)
    p_push.set_defaults(func=cmd_push)

    p_list = sub.add_parser("list", help="list stored skill packs")
    add_store_args(p_list)
    p_list.set_defaults(func=cmd_list)

    p_pull = sub.add_parser("pull", help="fetch a skill pack and extract it")
    p_pull.add_argument("name", help="skill name to pull")
    p_pull.add_argument(
        "dest", nargs="?", default=None, help="destination directory (default ./<name>)"
    )
    add_store_args(p_pull)
    p_pull.set_defaults(func=cmd_pull)

    return parser


def main(argv: list[str] | None = None) -> int:
    """
    CLI entry point.

    :param argv: Argument vector (defaults to ``sys.argv[1:]``).
    :returns: Process exit code.
    """
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
