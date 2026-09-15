"""Keep the guarded provider fixture alive for a native Electron recording."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("OMNIGENT_DISABLE_KEYRING") != "1":
        parser.error("OMNIGENT_DISABLE_KEYRING=1 is required")
    if os.environ.get("PYTHON_KEYRING_BACKEND") != "keyring.backends.null.Keyring":
        parser.error("PYTHON_KEYRING_BACKEND must select the null backend")
    if args.state.exists():
        parser.error("--state must name a fresh disposable directory")

    checkout = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(checkout))
    from tests._helpers.provider_setup_runtime import ProviderSetupRuntime

    runtime = ProviderSetupRuntime(args.state, checkout)
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        runtime.start()
        print(json.dumps({"url": runtime.url, "mock_port": runtime.mock_ports[1]}), flush=True)
        stopped.wait()
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
