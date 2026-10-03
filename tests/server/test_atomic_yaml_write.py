"""Regression test for non-atomic YAML writes to config.yaml.

``Path.write_text`` truncates the destination file before writing, leaving a
window where a concurrent reader observes an empty file.  ``yaml.safe_load``
on an empty string returns ``None``, which the spec parser rejects with
``OmnigentError: config.yaml must be a YAML mapping, got NoneType``.

The fix replaces ``write_text`` with a temp-file + ``os.replace`` dance
(``_atomic_write_yaml_mapping``), making every write appear atomically to any
concurrent reader.
"""

from __future__ import annotations

import threading
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Helpers mirroring old and new code
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Regression loop: N concurrent readers while a writer loops over the file
# ---------------------------------------------------------------------------

_PAYLOAD = {
    "spec_version": 1,
    "name": "test-agent",
    "executor": {"config": {"harness": "claude-sdk"}},
}
_ITERATIONS = 3000


def _count_empty_reads(write_fn, tmp_path: Path) -> int:
    """Return the number of reads that observed an empty/null YAML document."""
    config_path = tmp_path / "config.yaml"
    # Seed the file so readers never get FileNotFoundError.
    write_fn(config_path, _PAYLOAD)

    empty = []
    stop = threading.Event()

    def _writer() -> None:
        while not stop.is_set():
            write_fn(config_path, _PAYLOAD)

    def _reader() -> None:
        for _ in range(_ITERATIONS):
            text = config_path.read_text()
            if yaml.safe_load(text) is None:
                empty.append(1)

    writer_thread = threading.Thread(target=_writer)
    reader_threads = [threading.Thread(target=_reader) for _ in range(4)]

    for t in reader_threads:
        t.start()
    writer_thread.start()

    for t in reader_threads:
        t.join()
    stop.set()
    writer_thread.join()

    return len(empty)


def test_atomic_write_produces_no_empty_reads(tmp_path: Path) -> None:
    """The atomic writer never exposes an empty file to concurrent readers.

    Uses ``_atomic_write_yaml_mapping`` from ``session_mcp_servers`` so the
    test exercises the exact same helper that guards the production write paths.
    """
    from omnigent.server.routes.session_mcp_servers import _atomic_write_yaml_mapping

    empty_count = _count_empty_reads(_atomic_write_yaml_mapping, tmp_path)
    assert empty_count == 0, (
        f"{empty_count} concurrent reads observed an empty config.yaml; "
        "the atomic write is not working correctly."
    )
