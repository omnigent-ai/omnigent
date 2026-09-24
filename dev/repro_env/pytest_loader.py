"""Load optional evidence hooks without making their dependencies test prerequisites."""

import json
import os
import sys
import time
from pathlib import Path


def pytest_configure(config):
    directory = os.environ.get("OMNIGENT_REPRO_ATTEMPT_DIR")
    if not directory:
        return
    try:
        from . import pytest_evidence

        config.pluginmanager.register(pytest_evidence, "repro_evidence_collector")
    except Exception as exc:  # noqa: BLE001 — optional instrumentation must not abort pytest.
        config.pluginmanager.unregister(name="repro_evidence_collector")
        collector = getattr(config, "_repro_evidence", None)
        if collector is not None:
            collector.patch.undo()
            del config._repro_evidence
        error = {
            "time_ns": time.time_ns(),
            "kind": "collection_error",
            "operation": "pytest_plugin_load",
            "error_type": type(exc).__name__,
        }
        try:
            with (Path(directory) / f"events-plugin-{os.getpid()}.jsonl").open("a") as stream:
                stream.write(json.dumps(error) + "\n")
        except OSError:
            # Reporting failure must not prevent the original tests from running.
            pass
        print(
            f"reproduction evidence pytest_plugin_load failed: {type(exc).__name__}",
            file=sys.stderr,
        )
