"""resolve() must copy credential_broker spec->policy on both backends (plan Task 2b).

Without this bridge the feature silently no-ops: sandbox.credential_broker is
always None at every consumption site.
"""

import shutil
import sys

import pytest

from omnigent.inner.datamodel import CredentialBrokerSpec, OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.sandbox import resolve_sandbox


@pytest.mark.parametrize("backend", ["linux_bwrap", "darwin_seatbelt"])
def test_bridge_copies_broker_spec(backend, tmp_path):
    if backend == "linux_bwrap" and not (
        sys.platform.startswith("linux") and shutil.which("bwrap")
    ):
        pytest.skip("bwrap backend unavailable")
    if backend == "darwin_seatbelt" and sys.platform != "darwin":
        pytest.skip("seatbelt is darwin-only")
    spec = OSEnvSpec(
        type="caller_process",
        cwd=str(tmp_path),
        sandbox=OSEnvSandboxSpec(
            type=backend, allow_network=True, credential_broker=CredentialBrokerSpec()
        ),
    )
    policy = resolve_sandbox(spec, tmp_path)
    assert policy.credential_broker is spec.sandbox.credential_broker
