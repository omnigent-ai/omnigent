"""List and select real catalog entries, then simulate the launch lifecycle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

from omnigent.onboarding.sandboxes.kubernetes import KubernetesSandboxLauncher
from omnigent.onboarding.sandboxes.profiles import SandboxProfileContext
from omnigent.onboarding.sandboxes.registry import instantiate
from omnigent.onboarding.sandboxes.types import SandboxError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=str(Path(__file__).with_name("catalog.json")))
    parser.add_argument("--user", default="alice")
    parser.add_argument("--workspace", type=int, default=1)
    parser.add_argument("--pause-before-select", action="store_true")
    args = parser.parse_args()
    launcher = instantiate("profile_demo", config={"catalog_file": args.catalog})
    context = SandboxProfileContext(user_id=args.user, workspace_id=args.workspace)
    profiles = launcher.list_profiles(context)
    print(json.dumps([profile.model_dump() for profile in profiles], indent=2))
    if not profiles:
        print("No authorized profiles; no launch attempted.")
        return
    if args.pause_before_select:
        input("Edit the catalog's settings or permissions, then press Enter to select: ")
    launcher.select_profile(context, profiles[0])
    with (
        patch.object(KubernetesSandboxLauncher, "prepare") as prepare,
        patch.object(KubernetesSandboxLauncher, "provision", return_value="fake-job") as provision,
        patch.object(KubernetesSandboxLauncher, "resume") as resume,
    ):
        launcher.prepare_for_launch()
        launcher.prepare()
        sandbox_id = launcher.provision("demo")
        # Wake uses a fresh selection on the same launcher that will resume.
        launcher.select_profile(context, profiles[0])
        launcher.resume(sandbox_id)
        prepare.assert_called_once()
        provision.assert_called_once_with("demo")
        resume.assert_called_once_with("fake-job")
    print(f"Selected {profiles[0].id}; fake prepare/provision/resume passed.")
    print("No Kubernetes calls were made. Infrastructure was not tested.")


if __name__ == "__main__":
    try:
        main()
    except SandboxError as exc:
        raise SystemExit(f"{type(exc).__name__}: {exc}") from None
