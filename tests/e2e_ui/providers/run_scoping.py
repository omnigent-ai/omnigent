"""Run the guarded provider-scoping browser regression without unrelated conftests."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--recordings", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("OMNIGENT_DISABLE_KEYRING") != "1":
        parser.error("Run with OMNIGENT_DISABLE_KEYRING=1 and a clean environment")
    if os.environ.get("PYTHON_KEYRING_BACKEND") != "keyring.backends.null.Keyring":
        parser.error("Run with PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring")
    if args.state.exists():
        parser.error("--state must name a new disposable directory")

    import keyring
    from keyring.backends.null import Keyring
    from playwright.sync_api import sync_playwright

    keyring.set_keyring(Keyring())
    checkout = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(checkout))
    source = Path(__file__).with_name("test_provider_scoping.py")
    spec = importlib.util.spec_from_file_location("provider_scoping_test", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runtime = module.ProviderSetupRuntime(args.state, checkout)
    args.recordings.mkdir(parents=True, exist_ok=True)
    try:
        runtime.start()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                record_video_dir=str(args.recordings),
                record_video_size={"width": 1440, "height": 1000},
            )
            page = context.new_page()
            try:
                module.test_agent_scopes_defaults_detection_and_gateway_validation(
                    page, runtime, args.recordings
                )
                page.screenshot(path=str(args.recordings / "passed.png"), full_page=True)
            except BaseException:
                page.screenshot(path=str(args.recordings / "failed.png"), full_page=True)
                raise
            finally:
                context.close()
                browser.close()
        print(json.dumps({"result": "passed", "recordings": str(args.recordings)}))
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
