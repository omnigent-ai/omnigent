"""Public identity of this machine's running SSH service."""

from __future__ import annotations

import base64
import binascii
import hashlib
import subprocess
import sys


def ssh_host_key_fingerprints() -> list[str] | None:
    """Read loopback SSH public keys for matching a desktop's saved connections.

    Probe the running service: cloud images can retain unused keys in /etc/ssh.
    Missing SSH support must not prevent the host from connecting.
    """
    if sys.platform != "linux":
        return None
    try:
        result = subprocess.run(
            ["ssh-keyscan", "-T", "2", "-t", "ecdsa,ed25519,rsa", "127.0.0.1"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    else:
        fingerprints: set[str] = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 3 or fields[0] != "127.0.0.1":
                continue
            try:
                key = base64.b64decode(fields[2], validate=True)
            except (ValueError, binascii.Error):
                continue
            else:
                if key:
                    digest = base64.b64encode(hashlib.sha256(key).digest()).decode().rstrip("=")
                    fingerprints.add(f"SHA256:{digest}")
        return sorted(fingerprints) or None
