"""Build-output guard for the PWA embed island.

Companion to ``conftest._assert_pwa_build`` (which guards the *standalone* SPA
build in the ``built_spa`` fixture): this asserts the **embed island** ships no
service worker or web manifest. The island is mounted inside a host application
(e.g. Databricks), so it must never register a SW or precache anything — that is
why ``vite.embed.config.ts`` omits the ``emitPwaAssets`` plugin and never mounts
``PWAUpdateBanner``. A regression that added either would hijack the host page's
origin with our service worker.

Note: the install icons under ``public/`` *are* copied into the embed output
(Vite copies ``publicDir`` for every build) — but they are inert images. Only a
service worker or web manifest could affect the host's origin, so those (plus
the ``workbox-`` runtime) are precisely what this guard forbids.

Part of the gated e2e suite (needs ``npm`` + a vite build); see this package's
``conftest`` module docstring for how the suite is run and excluded from the
default ``pytest`` run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AP_WEB_DIR = _REPO_ROOT / "ap-web"


def test_embed_build_ships_no_service_worker(built_spa: None, tmp_path: Path) -> None:
    """The embed island build must emit no ``sw.js`` / manifest / workbox runtime.

    :param built_spa: Depended on only to guarantee the toolchain is installed
        (``npm ci``); the embed build does not use its standalone output.
    :param tmp_path: Isolated ``--outDir`` so this never clobbers a real build.
    :returns: None.
    """
    out = tmp_path / "embed"
    subprocess.run(
        ["npm", "run", "build:embed", "--", "--outDir", str(out)],
        cwd=_AP_WEB_DIR,
        check=True,
    )
    # Guard against a vacuous pass: if the `--outDir` override is ever dropped
    # (e.g. the `build:embed` script stops forwarding extra args), the build
    # lands elsewhere, `out` is empty, and the leak check below trivially passes
    # while testing nothing. Require the build to have actually emitted here.
    emitted = [p.name for p in out.rglob("*") if p.is_file()]
    assert emitted, (
        f"embed build produced no files in {out} — the --outDir override was not "
        "honored, so the no-service-worker/manifest assertion would pass vacuously"
    )
    leaked = sorted(
        p.name
        for p in out.rglob("*")
        if p.is_file()
        and (p.name == "sw.js" or p.suffix == ".webmanifest" or p.name.startswith("workbox-"))
    )
    assert not leaked, (
        f"embed island leaked PWA/service-worker assets {leaked} — the embed "
        "build must not emit a service worker or manifest (it loads inside a "
        "host app's origin)"
    )
