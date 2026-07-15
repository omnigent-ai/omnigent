"""Registration of operator-managed agent bundles."""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnigent.db.utils import generate_agent_id
from omnigent.spec import load, materialize_bundle


@dataclass(frozen=True)
class AgentRegistration:
    """Result of registering one operator-managed agent bundle."""

    agent_id: str
    name: str
    version: int
    changed: bool
    source: Path


def _add_bundle_to_tar(tar: tarfile.TarFile, bundle_dir: Path) -> None:
    """Add a bundle with stable ordering and metadata."""
    descendants = sorted(
        bundle_dir.rglob("*"),
        key=lambda path: path.relative_to(bundle_dir).as_posix(),
    )
    paths = [bundle_dir, *descendants]
    for path in paths:
        arcname = "." if path == bundle_dir else path.relative_to(bundle_dir).as_posix()
        info = tar.gettarinfo(str(path), arcname=arcname)
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        if info.isfile():
            with path.open("rb") as source_file:
                tar.addfile(info, source_file)
        else:
            tar.addfile(info)


def register_agent(
    agent_source: Path,
    agent_store: Any,
    artifact_store: Any,
    agent_cache: Any,
) -> AgentRegistration | None:
    """Validate and create or replace an operator-managed agent.

    The bundle is materialized and validated before any store is mutated.
    Replacements retain the existing agent ID so sessions continue to refer
    to the same template agent.

    :param agent_source: Agent directory or standalone Omnigent YAML file.
    :param agent_store: Store for agent metadata.
    :param artifact_store: Store for bundle bytes.
    :param agent_cache: Runtime bundle cache to refresh after replacements.
    :returns: Registration metadata, or ``None`` when the spec has no name.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        bundle_dir = materialize_bundle(agent_source, Path(tmpdir) / "bundle")

        buf = io.BytesIO()
        with (
            gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
            tarfile.open(fileobj=gz, mode="w") as tar,
        ):
            _add_bundle_to_tar(tar, bundle_dir)
        bundle_bytes = buf.getvalue()

        spec = load(bundle_dir)

    if spec.name is None:
        return None

    bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
    existing = agent_store.get_by_name(spec.name)
    if existing is not None:
        new_location = f"{existing.id}/{bundle_hash}"
        changed = existing.bundle_location != new_location
        version = existing.version
        if changed:
            artifact_store.put(new_location, bundle_bytes)
            updated = agent_store.update(existing.id, bundle_location=new_location)
            agent_cache.replace(
                existing.id,
                new_location,
                bundle_bytes,
                expand_env=True,
            )
            version = updated.version if updated is not None else existing.version + 1
        return AgentRegistration(
            agent_id=existing.id,
            name=spec.name,
            version=version,
            changed=changed,
            source=agent_source,
        )

    agent_id = generate_agent_id()
    location = f"{agent_id}/{bundle_hash}"
    artifact_store.put(location, bundle_bytes)
    created = agent_store.create(
        agent_id=agent_id,
        name=spec.name,
        bundle_location=location,
        description=spec.description,
    )
    return AgentRegistration(
        agent_id=agent_id,
        name=spec.name,
        version=created.version if created is not None else 1,
        changed=True,
        source=agent_source,
    )
