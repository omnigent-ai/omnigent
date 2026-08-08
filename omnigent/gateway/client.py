"""Loopback client for the host gateway servlet.

For same-machine callers that are not the harness (the model catalog, the
runner): discover the servlet through its state file and read the admin
catalog. Every failure returns ``None`` — callers fall open to whatever
source they used before the servlet existed.
"""

from __future__ import annotations

import logging

import httpx

from omnigent.gateway.auth import databrickscfg_host_for_profile
from omnigent.gateway.state import read_servlet_state

_logger = logging.getLogger(__name__)

_ADMIN_TIMEOUT_S = 5.0


def fetch_servlet_codex_slugs(profile: str | None) -> list[str] | None:
    """
    The servlet's routable codex slugs for a profile's workspace.

    The slugs are the servlet catalog's full servable set (Responses-dialect
    filtered, probe-validated arms included) in the spelling codex natively
    knows — the same inventory the ``/models`` picker and the launch default
    read.

    :param profile: ``~/.databrickscfg`` profile name, e.g. ``"oss"``.
    :returns: Slug list in catalog order, or ``None`` when no servlet is
        running, the profile resolves no workspace, or the catalog is
        unavailable (fail open).
    """
    if not profile:
        return None
    try:
        state = read_servlet_state()
        if state is None:
            return None
        workspace_host = databrickscfg_host_for_profile(profile)
        if workspace_host is None:
            return None
        response = httpx.get(
            f"{state.url}/admin/catalog",
            params={"profile": profile, "workspace_host": workspace_host},
            headers={"authorization": f"Bearer {state.admin_token}"},
            timeout=_ADMIN_TIMEOUT_S,
        )
        if response.status_code != 200:
            return None
        routable = response.json().get("routable_models")
        if not isinstance(routable, list):
            return None
        slugs = [slug for slug in routable if isinstance(slug, str) and slug]
        return slugs or None
    except Exception:  # noqa: BLE001 — discovery is best-effort by design
        _logger.debug("gateway servlet catalog unavailable", exc_info=True)
        return None
