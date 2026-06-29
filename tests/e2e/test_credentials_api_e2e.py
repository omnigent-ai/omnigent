"""E2E: the per-user secret vault REST round-trips on the live server (#5).

Stores a secret via ``PUT /v1/credentials/{name}`` and reads the listing back —
proving the vault store + Fernet + routes are wired end to end and that listings
return metadata only (never the secret). Two-user isolation + the acting-user
injection path are covered in-process by
``tests/server/integration/test_credentials_routes.py`` and
``tests/inner/test_os_env_credential_injection.py``.
"""

from __future__ import annotations

import httpx


def test_credential_put_then_list_metadata_only(http_client: httpx.Client) -> None:
    put = http_client.put("/v1/credentials/github", json={"secret": "ghp_e2e_secret_value"})
    put.raise_for_status()
    assert "ghp_e2e_secret_value" not in put.text  # never echoed back

    listing = http_client.get("/v1/credentials").json()["data"]
    names = [c["name"] for c in listing]
    assert "github" in names
    # The secret value must not appear anywhere in the listing payload.
    assert all("ghp_e2e_secret_value" not in str(c) for c in listing)
