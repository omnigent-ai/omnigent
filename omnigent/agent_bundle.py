"""Stdlib-only agent bundle helper.

Extracted from ``omnigent.cli`` so that host-process code (and the server)
can call :func:`bundle_directory` without dragging in click / rich / CLI
sandbox groups.

Imports: stdlib only (``io``, ``tarfile``, ``pathlib``).
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path


def bundle_directory(source: Path, resolved: dict[str, str] | None = None) -> bytes:
    """Tar+gzip a directory tree into agent-bundle bytes.

    :param source: Directory containing ``config.yaml`` (+ optional AGENTS.md, skills/).
    :param resolved: Optional map of arcname -> resolved YAML content to substitute
        (used by the CLI to pre-expand ``${VAR}``). ``None`` packs files verbatim.
    :returns: The gzipped tarball bytes.
    """
    resolved = resolved or {}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for file_path in source.rglob("*"):
            if file_path.is_file():
                arcname = str(file_path.relative_to(source))
                if arcname in resolved:
                    data = resolved[arcname].encode("utf-8")
                    info = tarfile.TarInfo(name=arcname)
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
                else:
                    tf.add(str(file_path), arcname=arcname)
    return buf.getvalue()
