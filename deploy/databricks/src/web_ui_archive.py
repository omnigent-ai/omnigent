"""Safe extraction for the Databricks Apps web UI archive."""

from __future__ import annotations

import tarfile
from pathlib import Path

# These bounds apply after gzip expansion. The compressed archive is separately
# checked against Databricks Apps' 10 MiB source-file limit by deploy.py.
MAX_MEMBERS = 4096
MAX_MEMBER_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024


def extract_web_ui_archive(archive: Path, destination: Path) -> None:
    """Safely extract a bounded web UI archive into ``destination``."""
    with tarfile.open(archive, "r:gz") as tar:
        members = []
        total_size = 0
        # Iterate headers and validate each one before advancing over its data.
        # This rejects a single large member before reading its compressed body.
        for member in tar:
            members.append(member)
            if len(members) > MAX_MEMBERS:
                raise ValueError(f"archive has too many members ({len(members)})")
            if member.isfile():
                if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                    raise ValueError("archive contains a file over the 10 MB limit")
                total_size += member.size
                if total_size > MAX_EXTRACTED_BYTES:
                    raise ValueError("archive expands beyond the web UI size limit")
        # The data filter rejects absolute paths, traversal, links, and special
        # files that could escape or mutate state outside the extraction root.
        tar.extractall(destination, members=members, filter="data")
