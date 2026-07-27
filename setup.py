"""Custom setuptools build for omnigent.

Generates ``omnigent/_build_info.py`` at wheel build time so the
CLI's update-check (``omnigent/update_check.py``) can tell the user
when their installed build is stale without having to consult
``git`` or hit a remote endpoint at startup.

All other build configuration lives in ``pyproject.toml``; this
file exists solely to register the cmdclass override that runs the
generator before ``build_py`` copies sources into the wheel.

The generated file is gitignored — it is recreated on every build
and only meaningful at install time, where it travels inside the
wheel alongside the rest of the package.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class _GenerateBuildInfo(build_py):
    """Subclass of ``build_py`` that writes ``_build_info.py``.

    The override is the smallest possible intervention: run the
    generator, then defer to the stock ``build_py`` to copy sources
    (including the freshly-written ``_build_info.py``) into the
    wheel's build directory. No other behavior of the build is
    changed.
    """

    def run(self) -> None:
        """Build the web UI, generate ``_build_info.py``, then run build_py."""
        self._build_web_ui()
        self._write_build_info()
        super().run()
        self._bundle_examples()
        self._bundle_scripts()

    def _bundle_scripts(self) -> None:
        """Copy top-level maintenance scripts into package resources."""
        import shutil

        root = Path(__file__).resolve().parent
        src = root / "scripts" / "uninstall_oss.sh"
        if not src.is_file():
            return
        dest = Path(self.build_lib) / "omnigent" / "resources" / "scripts" / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    def _bundle_examples(self) -> None:
        """Copy bundled example agents into the wheel as real directories.

        ``omnigent/resources/examples/{polly,debby}`` may exist as symlinks
        into the top-level ``examples/`` tree (or not at all) depending on
        the checkout, and setuptools' ``package-data`` never materializes
        symlinks into the built wheel — a directory symlink is not walked.
        A plain ``pip install`` / ``uv tool install`` would then ship a
        package whose ``omnigent.resources.examples`` has no ``polly`` /
        ``debby`` subdir, and bare ``omnigent`` (first-run default → polly)
        dies with "Agent path not found".

        Fix: after ``build_py`` has populated ``build_lib``, copy the real
        example trees from the top-level ``examples/`` dir (present in every
        checkout) into
        ``build_lib/omnigent/resources/examples/<name>`` so every wheel is
        self-contained. This honors the contract documented in cli.py's
        ``_bundled_polly_path``: a symlink in a checkout, a real directory in
        an installed wheel. Editable installs (``uv sync``) resolve the
        in-checkout symlink directly and don't need this.
        """
        import shutil

        root = Path(__file__).resolve().parent
        dest_root = Path(self.build_lib) / "omnigent" / "resources" / "examples"
        for name in ("debby", "polly"):
            src = root / "examples" / name
            if not src.is_dir():
                continue
            dst = dest_root / name
            if dst.is_symlink() or dst.is_file():
                dst.unlink()
            elif dst.is_dir():
                shutil.rmtree(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst)

    def _build_web_ui(self) -> None:
        """Build the web SPA into ``omnigent/server/static/web-ui/``.

        The server mounts that directory at ``/`` when present
        (``omnigent/server/app.py``); when absent it serves an
        API-only JSON landing page and the web UI is unreachable.
        The bundle is npm-build output, not tracked in git, so a
        plain ``pip install .`` / ``uv tool install`` from a checkout
        would otherwise ship no UI — the single most common "the web
        UI doesn't load" report.

        Build policy, chosen to fix that case without slowing the
        backend-only dev loop or breaking node-less CI:

        - Skip if ``web/`` is absent (sdists that don't vendor it).
        - Skip if ``OMNIGENT_SKIP_WEB_UI=true``. The hardened CI
          runners ship a system ``npm`` but have no fast registry
          mirror configured for the lint/test shards, so ``npm
          install`` crawls against the public registry and hits the
          600s timeout — 10 wasted minutes per ``uv sync`` for a
          bundle those jobs never serve. They set this env var to opt
          out.
        - Skip if the bundle already exists, UNLESS
          ``OMNIGENT_BUILD_WEB_UI=1`` forces a rebuild. This keeps
          repeat ``uv sync`` fast for backend devs (build once, reuse)
          while letting release builds force a fresh bundle.
        - Otherwise the build MUST succeed: a missing ``npm`` or a
          failing ``npm install`` / ``npm run build`` aborts the
          install with an actionable error. Omnigent needs Node +
          npm at runtime anyway (the Claude / Codex / Pi harness
          CLIs are npm packages), so a node-less machine would get a
          broken install either way — failing here, with a message
          that says how to fix it, beats a silent API-only install
          that surfaces later as "the web UI doesn't load".

        :raises SystemExit: If ``npm`` is not on PATH or the web UI
            build fails, and no skip condition applies.
        """
        import os
        import shutil

        root = Path(__file__).resolve().parent
        web_src = root / "web"
        bundle = root / "omnigent" / "server" / "static" / "web-ui" / "index.html"

        if not (web_src / "package.json").is_file():
            return
        # CI opt-out: exact "true" only — this is set by our own
        # workflows, not user-facing config.
        if os.environ.get("OMNIGENT_SKIP_WEB_UI") == "true":
            return
        force_raw = os.environ.get("OMNIGENT_BUILD_WEB_UI")
        force = force_raw is not None and force_raw.strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if bundle.is_file() and not force:
            return
        npm = shutil.which("npm")
        if npm is None:
            raise SystemExit(
                "omnigent build: npm not found on PATH, so the web UI "
                "cannot be built. Omnigent requires Node.js 22 LTS or "
                "newer with npm (the Claude / Codex / Pi harness CLIs are "
                "npm packages). Install it from "
                "https://nodejs.org/en/download and rerun the install. "
                "To deliberately install without the web UI (API-only "
                "server), set OMNIGENT_SKIP_WEB_UI=true."
            )
        try:
            subprocess.run([npm, "install"], cwd=web_src, check=True, timeout=600)
            subprocess.run([npm, "run", "build"], cwd=web_src, check=True, timeout=600)
        except (subprocess.SubprocessError, OSError) as exc:
            raise SystemExit(
                f"omnigent build: web UI build failed ({exc}). Fix the "
                "failure above (it usually means Node.js is older than the "
                "required 22 LTS, or `npm install` could not reach the npm "
                "registry) and rerun the install. To deliberately install "
                "without the web UI (API-only server), set "
                "OMNIGENT_SKIP_WEB_UI=true."
            ) from exc

    def _write_build_info(self) -> None:
        """Write ``omnigent/_build_info.py`` into the source tree.

        Writing to the source tree (rather than directly into the
        build dir) means editable installs (``pip install -e .``,
        ``uv sync``) also get the file — they're a single
        ``build_py`` invocation against an in-place package — and
        any later non-build code path that does ``from omnigent
        import _build_info`` works without re-running the build.
        """
        target = Path(__file__).resolve().parent / "omnigent" / "_build_info.py"
        commit = _git_sha()
        # Use repr() for the SHA so quoting is always correct, even
        # for an empty fallback. The format is deliberately minimal
        # — anything more elaborate (version strings, branch names)
        # belongs in pyproject.toml or git tags, not here.
        target.write_text(
            '"""Auto-generated at wheel build time; do not edit.\n\n'
            "This module is created by ``setup.py`` immediately before\n"
            "``build_py`` packages the wheel, and is gitignored so it\n"
            "is recreated on every build. Consumers should import it\n"
            "defensively (``try: from omnigent import _build_info``)\n"
            "because source checkouts that have never been built will\n"
            "not have it on disk.\n"
            '"""\n'
            "from __future__ import annotations\n\n"
            f"BUILD_TIME_EPOCH: int = {int(time.time())}\n"
            f"COMMIT_SHA: str = {commit!r}\n"
        )


def _git_sha() -> str:
    """Return the current Git HEAD SHA, or empty string on failure.

    Empty-string fallback is intentional: when this is run inside a
    Docker build context with no ``git`` binary, or when the build
    happens from an sdist that has no ``.git/`` directory, the field
    must still be populated with a stable string so the generated
    module remains importable. The CLI update-check treats an empty
    SHA as "no commit info available" and silently falls back to
    timestamp-only nag logic.

    :returns: 40-character full hex SHA, or ``""`` on any failure.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return result.stdout.strip()


setup(cmdclass={"build_py": _GenerateBuildInfo})
