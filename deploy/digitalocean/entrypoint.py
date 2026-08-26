"""Generate App Platform's ephemeral server config, then run the Docker entrypoint."""

from __future__ import annotations

import os
import runpy
import tempfile
from pathlib import Path
from typing import Any

import yaml


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def generate_config() -> Path:
    """Write non-secret DigitalOcean settings to a new temporary YAML file."""
    public_url = os.environ.get("OMNIGENT_PUBLIC_URL", "").strip().rstrip("/")
    if not public_url.startswith("https://"):
        raise RuntimeError("OMNIGENT_PUBLIC_URL must be the public https:// App Platform URL")
    mount_path = os.environ.get("DIGITALOCEAN_WORKSPACE_MOUNT_PATH", "/workspace").strip()
    if not mount_path.startswith("/") or mount_path == "/":
        raise RuntimeError("DIGITALOCEAN_WORKSPACE_MOUNT_PATH must be an absolute non-root path")

    config: dict[str, Any] = {
        "artifact_location": "/tmp/omnigent-artifacts",
        "sandbox": {
            "provider": "digitalocean",
            "server_url": public_url,
            "digitalocean": {
                "region": os.environ.get("DIGITALOCEAN_REGION", "sgp1").strip(),
                "size": os.environ.get("DIGITALOCEAN_DROPLET_SIZE", "s-4vcpu-8gb").strip(),
                "image": os.environ.get("DIGITALOCEAN_DROPLET_IMAGE", "ubuntu-24-04-x64").strip(),
                "host_image": os.environ.get(
                    "OMNIGENT_DIGITALOCEAN_HOST_IMAGE",
                    "ghcr.io/omnigent-ai/omnigent-host:latest",
                ).strip(),
                "workspace": {
                    "size_gb": _positive_int("DIGITALOCEAN_WORKSPACE_SIZE_GB", 100),
                    "mount_path": mount_path.rstrip("/"),
                },
            },
        },
    }
    config_dir = Path(tempfile.mkdtemp(prefix="omnigent-digitalocean-"))
    config_path = config_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
    os.chmod(config_path, 0o600)
    os.environ["OMNIGENT_CONFIG"] = str(config_path)
    return config_path


def main() -> None:
    generate_config()
    runpy.run_path("/app/entrypoint.py", run_name="__main__")


if __name__ == "__main__":
    main()
