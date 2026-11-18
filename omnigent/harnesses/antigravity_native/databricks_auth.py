"""Resolve a selected Databricks profile in an isolated auth process."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from omnigent.errors import ErrorCode, OmnigentError


def validate_workspace_host(host: str) -> str:
    """Require a workspace HTTPS origin before authentication or token release."""
    host = host.strip().rstrip("/")
    try:
        parsed = urlsplit(host)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise OmnigentError(
            "Databricks profile host must be an HTTPS workspace root without "
            "credentials, a path, a query, or a fragment.",
            code=ErrorCode.INVALID_INPUT,
        )
    return host


@dataclass
class ProfileAuthConfig:
    """SDK-compatible auth interface, keeping environment changes out of the daemon."""

    profile: str
    host: str
    environment: dict[str, str] = field(default_factory=os.environ.copy, repr=False)

    def authenticate(self) -> dict[str, str]:
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-P",
                    "-m",
                    __name__,
                    "--profile",
                    self.profile,
                    "--host",
                    self.host,
                ],
                env=self.environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("Databricks profile authentication failed or timed out.") from None
        token = result.stdout.strip()
        if result.returncode != 0 or not token:
            raise ValueError(
                f"Cannot authenticate Databricks profile {self.profile!r}. "
                "Check its credentials or refresh its CLI OAuth login with "
                f"`databricks auth login --profile {self.profile}`."
            )
        return {"Authorization": f"Bearer {token}"}


def main() -> int:
    import argparse

    from omnigent.inner.databricks_executor import _read_databrickscfg_host
    from omnigent.inner.databricks_token import _sdk_bearer

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--host", required=True)
    args = parser.parse_args()
    try:
        expected_host = validate_workspace_host(args.host)
        if validate_workspace_host(_read_databrickscfg_host(args.profile) or "") != expected_host:
            return 1
        # This helper scrubs ambient SDK credentials and changes the socket timeout.
        # Run it only in this disposable process, including on token refresh.
        resolved = _sdk_bearer(args.profile, None)
    except Exception:  # Keep SDK diagnostics and credentials off stdout.
        return 1
    if resolved is None:
        return 1
    host, token = resolved
    if not token or host.strip().rstrip("/") != expected_host:
        return 1
    sys.stdout.write(token + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
