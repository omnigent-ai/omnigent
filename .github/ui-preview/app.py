"""Entry point for the per-PR UI Preview app (Databricks Apps).

Port of MLflow's ``.github/ui-preview/app.py`` adapted to Omnigent.

Unlike Omnigent's production Databricks deploy (``deploy/databricks/``, which
uses Lakebase Postgres + UC Volumes), this preview is deliberately *ephemeral
and self-contained* so a fresh app can be created and torn down per PR with no
external state: a SQLite database + local-disk artifact store under a temp dir.

There is no bundled LLM or runner. Omnigent executes agent turns on a runner
that the user connects from their own machine/sandbox (``omnigent run … --server
<url>``), so the preview only needs to serve the web UI + API. A reviewer browses
the UI as-is, and can connect their own host to drive a real session.

The prebuilt web SPA is shipped separately as ``build.tar.gz`` (keeping the
wheel small, like MLflow) and extracted into the installed ``omnigent`` package
so the server mounts it at ``/``.
"""

from __future__ import annotations

import logging
import os
import sys
import tarfile
from pathlib import Path

logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True)
logger = logging.getLogger("omnigent-ui-preview")

HERE = Path(__file__).parent.resolve()
# Databricks Apps expects the app to listen on DATABRICKS_APP_PORT (8000 by
# convention); fall back to 8000 for local runs of this script.
PORT = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
WORK_DIR = Path(os.environ.get("OMNIGENT_PREVIEW_WORKDIR", "/tmp/omnigent-preview"))
DB_PATH = WORK_DIR / "omnigent.db"
ARTIFACT_DIR = WORK_DIR / "artifacts"


def _extract_spa() -> None:
    """Extract the prebuilt SPA into the installed omnigent package.

    The build job ships ``build.tar.gz`` (containing a ``web-ui`` dir) next to
    this file; the server serves ``omnigent/server/static/web-ui`` at ``/``.
    """
    tar_path = HERE / "build.tar.gz"
    if not tar_path.is_file():
        logger.warning("No build.tar.gz found at %s -- UI will be API-only", tar_path)
        return
    import omnigent.server

    target = Path(omnigent.server.__file__).parent / "static"
    target.mkdir(parents=True, exist_ok=True)
    logger.info("Extracting SPA from %s into %s", tar_path, target)
    with tarfile.open(tar_path) as tar:
        tar.extractall(target)


def main() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    _extract_spa()

    # The Databricks Apps proxy injects X-Forwarded-Email on every request, so
    # run in header auth mode (matches deploy/databricks/src/app.py) -- no login
    # page, and the proxy is the trust boundary.
    os.environ.setdefault("OMNIGENT_AUTH_PROVIDER", "header")

    cmd = [
        sys.executable, "-m", "omnigent.cli", "server",
        "--host", "0.0.0.0",
        "--port", str(PORT),
        "--database-uri", f"sqlite:///{DB_PATH}",
        "--artifact-location", str(ARTIFACT_DIR),
        "--no-open",
    ]
    logger.info("Starting Omnigent server: %s", " ".join(cmd))
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
