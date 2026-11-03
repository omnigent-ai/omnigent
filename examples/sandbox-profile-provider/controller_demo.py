"""Exercise the HTTP adapter against the local simulator or a compatible test controller."""

import argparse
from dataclasses import replace

from omnigent.community.sandbox.profile_demo.controller import (
    ControllerConfig,
    ControllerSandboxLauncher,
)
from omnigent.onboarding.sandboxes.profiles import SandboxProfileContext

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--url", default="http://127.0.0.1:18991")
parser.add_argument(
    "--sandbox", help="Recover an existing sandbox, including after controller restart"
)
args = parser.parse_args()
launcher = ControllerSandboxLauncher(config=ControllerConfig(url=args.url))
context = SandboxProfileContext(user_id="alice", workspace_id=0)
if args.sandbox:
    sandbox_id = args.sandbox
else:
    choices = launcher.list_profiles(context)
    launcher.select_profile(context, choices[0])
    sandbox_id = launcher.provision("profile-recovery-demo")
# Reconstruct the launcher without any session reference.
launcher = ControllerSandboxLauncher(config=ControllerConfig(url=args.url))
scoped = replace(context, sandbox_id=sandbox_id)
[profile] = launcher.list_profiles(scoped)
launcher.select_profile(scoped, profile)
launcher.resume(sandbox_id)
print(f"Recovered {sandbox_id}: {profile.id}@{profile.revision}")
print(
    "Restart the controller with the same --state file, then run this script "
    f"with --sandbox {sandbox_id}"
)
