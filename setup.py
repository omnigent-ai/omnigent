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
        The bundle is Vite build output, not tracked in git, so a
        plain ``pip install .`` / ``uv tool install`` from a checkout
        would otherwise ship no UI — the single most common "the web
        UI doesn't load" report.

        ``web/`` is a package in a pnpm workspace (``pnpm-workspace.yaml``
        and ``pnpm-lock.yaml`` at the repo root, ``packageManager:
        pnpm@11.15.1`` in the root ``package.json``), so the install and
        build run against the **workspace root** with ``--filter web``,
        matching ``deploy/databricks/build.sh`` and the CI workflows.
        Running ``pnpm install`` from inside ``web/`` would miss the
        committed lockfile and resolve against ``package.json`` alone —
        the legacy npm path that hit peer-dependency conflicts.

        Build policy, chosen to fix that case without slowing the
        backend-only dev loop or breaking node-less CI:

        - Skip if ``web/`` is absent (sdists that don't vendor it).
        - Skip if ``OMNIGENT_SKIP_WEB_UI=true``. The hardened CI
          runners ship pnpm but have no fast registry mirror
          configured for the lint/test shards, so ``pnpm install``
          crawls against the public registry and hits the 600s
          timeout — 10 wasted minutes per ``uv sync`` for a bundle
          those jobs never serve. They set this env var to opt out.
        - Skip if the bundle already exists, UNLESS
          ``OMNIGENT_BUILD_WEB_UI=1`` forces a rebuild. This keeps
          repeat ``uv sync`` fast for backend devs (build once, reuse)
          while letting release builds force a fresh bundle.
        - Otherwise the build MUST succeed: a missing Node.js 22+,
          a missing pnpm, or a failing ``pnpm install`` / ``pnpm
          --filter web run build`` aborts the install with an
          actionable error. Omnigent needs Node 22 LTS + pnpm at
          runtime anyway (the Claude / Codex / Pi harness CLIs are
          npm packages, and the web UI is a pnpm workspace), so a
          node-less machine would get a broken install either way —
          failing here, with a message that says how to fix it, beats
          a silent API-only install that surfaces later as "the web
          UI doesn't load".

        The pnpm commands run with ``COREPACK_ENABLE_DOWNLOAD_PROMPT=0``
        and no stdin: corepack (whether reached via its ``pnpm`` shim or
        as ``corepack pnpm``) otherwise blocks on an interactive
        confirmation before downloading the pinned pnpm, which a
        non-interactive install can never answer.

        :raises SystemExit: If Node.js < 22 (or absent), pnpm is not
            on PATH (and corepack can't supply it), or the web UI
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
        # Enforce the Node.js 22 LTS floor up front. The web UI's
        # toolchain (pnpm@11, Vite 8, oxlint) and the runtime harness
        # CLIs all require Node 22; building on an older Node fails
        # deep inside the toolchain with an opaque error, so we fail
        # fast here with a single actionable message instead.
        _require_node_22()

        # pnpm first; fall back to corepack (bundled with Node 22+),
        # which downloads the pnpm version pinned by the root
        # package.json's ``packageManager`` field on first use.
        pnpm = shutil.which("pnpm")
        if pnpm is not None:
            pnpm_cmd = [pnpm]
        else:
            corepack = shutil.which("corepack")
            if corepack is not None:
                pnpm_cmd = [corepack, "pnpm"]
            else:
                pnpm_cmd = None
        if pnpm_cmd is None:
            raise SystemExit(
                "omnigent build: pnpm not found on PATH, so the web UI "
                "cannot be built. Omnigent requires Node.js 22 LTS or "
                "newer with pnpm (the web UI is a pnpm workspace; the "
                "Claude / Codex / Pi harness CLIs are npm packages). "
                "Install Node from https://nodejs.org/en/download and "
                "enable pnpm with `corepack enable` (or `npm install -g "
                "pnpm`), then rerun the install. To deliberately install "
                "without the web UI (API-only server), set "
                "OMNIGENT_SKIP_WEB_UI=true."
            )
        # A ``pnpm`` on PATH is often a corepack shim, which asks "Do you
        # want to continue? [Y/n]" before fetching the pinned pnpm. That
        # prompt is invisible under a build backend, so the install looks
        # hung; "0" downloads without asking (shims default it to "1").
        env = {**os.environ, "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"}
        try:
            # Workspace root, not ``web/``: the lockfile and workspace
            # manifest live at the repo root. ``--frozen-lockfile``
            # matches CI and guarantees the build is reproducible from
            # the committed ``pnpm-lock.yaml``. No stdin, so nothing in
            # the toolchain can block on input we can never deliver.
            subprocess.run(
                [*pnpm_cmd, "install", "--frozen-lockfile", "--filter", "web"],
                cwd=root,
                check=True,
                timeout=600,
                stdin=subprocess.DEVNULL,
                env=env,
            )
            subprocess.run(
                [*pnpm_cmd, "--filter", "web", "run", "build"],
                cwd=root,
                check=True,
                timeout=600,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            raise SystemExit(
                f"omnigent build: web UI build failed ({exc}). Fix the "
                "failure above (it usually means Node.js is older than "
                "the required 22 LTS, pnpm is missing, or `pnpm install` "
                "could not reach the npm registry) and rerun the install. "
                "To deliberately install without the web UI (API-only "
                "server), set OMNIGENT_SKIP_WEB_UI=true."
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
        # Keep generated names and types aligned with omnigent/_build_info.pyi.
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


def _require_node_22() -> None:
    """Abort the build unless Node.js >= 22 is on PATH.

    The web UI toolchain (pnpm 11, Vite 8, oxlint) and the runtime
    harness CLIs (Claude / Codex / Pi, all npm packages) require Node 22
    LTS. Building on an older Node fails deep inside the toolchain with
    an opaque error; this check fails fast with a single actionable
    message instead.

    :raises SystemExit: If ``node`` is missing or reports a major
        version below 22.
    """
    import shutil

    node = shutil.which("node")
    if node is None:
        raise SystemExit(
            "omnigent build: Node.js not found on PATH, so the web UI "
            "cannot be built. Omnigent requires Node.js 22 LTS or newer "
            "(the web UI toolchain — pnpm 11 / Vite 8 / oxlint — and the "
            "Claude / Codex / Pi harness CLIs all need it). Install it "
            "from https://nodejs.org/en/download (the 22 LTS line or "
            "newer) and rerun the install. To deliberately install "
            "without the web UI (API-only server), set "
            "OMNIGENT_SKIP_WEB_UI=true."
        )
    try:
        result = subprocess.run(
            [node, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise SystemExit(
            f"omnigent build: could not determine the Node.js version "
            f"(`node --version` failed: {exc}). Omnigent requires "
            "Node.js 22 LTS or newer. Ensure Node 22+ is installed and "
            "on PATH, then rerun the install. To deliberately install "
            "without the web UI (API-only server), set "
            "OMNIGENT_SKIP_WEB_UI=true."
        ) from exc
    # ``node --version`` prints ``v22.14.0``; split off the leading ``v``
    # and read the major. A non-numeric result is treated as too old.
    version_str = result.stdout.strip().lstrip("v")
    try:
        major = int(version_str.split(".")[0])
    except ValueError:
        major = -1
    if major < 22:
        raise SystemExit(
            f"omnigent build: Node.js {version_str or 'unknown'} is "
            f"installed, but Omnigent requires Node.js 22 LTS or newer "
            "(the web UI toolchain — pnpm 11 / Vite 8 / oxlint — and "
            "the Claude / Codex / Pi harness CLIs all need Node 22+). "
            "Upgrade from https://nodejs.org/en/download (pick the 22 "
            "LTS line or newer) and rerun the install. To deliberately "
            "install without the web UI (API-only server), set "
            "OMNIGENT_SKIP_WEB_UI=true."
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
