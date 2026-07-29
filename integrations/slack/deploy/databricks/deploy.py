#!/usr/bin/env python3
"""Deploy the Omnigent Slack bot to a Databricks App via Asset Bundles.

Builds a wheel for the ``omnigent-slack`` package, generates an app-level
``pyproject.toml`` that depends on that wheel (with the bot's runtime deps
inlined from the source ``pyproject.toml``), copies the wheel into ``src/``,
then wraps ``databricks bundle deploy`` + ``databricks bundle run``.

No lockfile is generated or committed: the app starts with ``uv run``, so the
Databricks Apps runtime resolves dependencies in-container at boot (the same
pattern as the ``databricks/app-templates`` examples). This keeps the deploy
step offline-simple — no ``uv lock``, no registry normalization, no
``--exclude-newer`` juggling to match the runtime's pinned cutoff.

Simpler than the server deploy: one wheel, pure-PyPI deps, no Lakebase / UC
volume and no cross-package version lockstep.

Runs unchanged from a laptop or CI. Re-runnable; every step is idempotent.

Usage:
    uv run python integrations/slack/deploy/databricks/deploy.py \\
        --app-name omnigent-slack --profile <your-profile> \\
        --secret-scope omnigent-slack \\
        --server-url https://<server-app>.databricksapps.com

See ``README.md`` in this directory for the full guide (secret scope
creation, user-authorization enablement, and the enrollment flow).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import tomllib

# Must match resources.apps.<key> and bundle.name in databricks.yml.
_BUNDLE_RESOURCE_KEY = "omnigent-slack"

# Distribution / import names of the package being deployed.
_DIST_NAME = "omnigent-slack"
_WHEEL_PREFIX = "omnigent_slack-"

_APP_REQUIRES_PYTHON = ">=3.12,<3.13"


def _log(msg: str) -> None:
    print(f"[deploy-slack] {msg}", flush=True)


def _fail(msg: str) -> None:
    print(f"[deploy-slack] ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def _deploy_dir() -> Path:
    return Path(__file__).resolve().parent


def _slack_root() -> Path:
    # integrations/slack/deploy/databricks/deploy.py → integrations/slack
    return Path(__file__).resolve().parents[2]


def _src_dir() -> Path:
    return _deploy_dir() / "src"


def _read_base_version() -> str:
    """Read the package's base version from its pyproject.toml."""
    text = (_slack_root() / "pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        _fail("could not find version in integrations/slack/pyproject.toml")
    return match.group(1)  # type: ignore[union-attr]


def _compute_deploy_version(base: str, explicit: str | None) -> str:
    """Stamp a unique per-deploy version so the wheel cache never collides.

    Mirrors the server deploy: strip any existing suffix, then append
    ``.post<unix-ts>`` (a final release that sorts above the base).
    """
    if explicit:
        if not re.match(r"^\d+(\.\d+)*(\.dev\d+|\.post\d+|[+\-][\w.]+)?$", explicit):
            _fail(f"--version {explicit!r} is not a recognizable PEP 440 version")
        return explicit
    base = re.sub(r"(\.post\d+|\.dev\d+)+$", "", base)
    return f"{base}.post{int(time.time())}"


def _stamp_version(new_version: str) -> str:
    """Rewrite the package version line; return the original text for restore."""
    path = _slack_root() / "pyproject.toml"
    original = path.read_text()
    updated, count = re.subn(
        r'(?m)^version\s*=\s*"[^"]+"',
        f'version = "{new_version}"',
        original,
        count=1,
    )
    if count != 1:
        _fail("could not rewrite version in integrations/slack/pyproject.toml")
    path.write_text(updated)
    return original


def _build_wheel() -> Path:
    """Build the omnigent-slack wheel into the package's dist/, return its path."""
    root = _slack_root()
    dist = root / "dist"
    # Sweep stale wheels so we pick exactly the one we just built.
    for old in dist.glob(f"{_WHEEL_PREFIX}*.whl"):
        old.unlink()
    _log("uv build --wheel")
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(dist), str(root)], check=True)
    wheels = sorted(dist.glob(f"{_WHEEL_PREFIX}*.whl"))
    if len(wheels) != 1:
        _fail(f"expected exactly one {_WHEEL_PREFIX} wheel, found {len(wheels)}")
    return wheels[0]


def _sweep_src_wheels() -> None:
    """Delete stale wheels from src/ so old versions can't linger in the sync."""
    src = _src_dir()
    for entry in src.glob(f"{_WHEEL_PREFIX}*.whl"):
        _log(f"removing stale wheel src/{entry.name}")
        entry.unlink()


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _read_runtime_dependencies() -> list[str]:
    """Read the bot's runtime deps from its source pyproject.toml.

    Inlining them into the generated app pyproject (rather than relying on the
    wheel's own metadata) keeps the app's dependency set visible and in lockstep
    with the package: ``uv run`` resolves this list plus the wheel in-container.
    """
    data = tomllib.loads((_slack_root() / "pyproject.toml").read_text())
    deps = data.get("project", {}).get("dependencies", [])
    if not deps:
        _fail("no [project.dependencies] found in integrations/slack/pyproject.toml")
    return list(deps)


def _write_app_pyproject(wheel: Path, deploy_version: str) -> None:
    """Copy the wheel into src/ and write the app pyproject.toml (no lockfile).

    The app starts with ``uv run``, so the Apps runtime resolves this project
    in-container at boot — no ``uv.lock`` is generated or shipped. The generated
    project pins the bot to the co-located wheel and inlines its runtime deps
    (read from the source pyproject) so the resolved set is explicit and stays in
    sync with the package.
    """
    src = _src_dir()
    _sweep_src_wheels()
    shutil.copy2(wheel, src / wheel.name)
    _log(f"copied {wheel.name} → src/")

    # A stale requirements.txt would make the runtime prefer pip over uv.
    requirements = src / "requirements.txt"
    if requirements.exists():
        requirements.unlink()
    # A stale lockfile from an older wheel-and-lock deploy would pin the wrong
    # version; drop it so `uv run` resolves fresh in-container.
    lockfile = src / "uv.lock"
    if lockfile.exists():
        lockfile.unlink()

    deps = [f"{_DIST_NAME}=={deploy_version}", *_read_runtime_dependencies()]
    dep_lines = "".join(f"  {_toml_string(d)},\n" for d in deps)
    pyproject = (
        "[project]\n"
        'name = "omnigent-slack-databricks-app"\n'
        'version = "0.0.0"\n'
        f"requires-python = {_toml_string(_APP_REQUIRES_PYTHON)}\n"
        "dependencies = [\n"
        f"{dep_lines}"
        "]\n\n"
        # Not an installable package itself — just an environment for `uv run`.
        "[tool.uv]\n"
        "package = false\n\n"
        "[tool.uv.sources]\n"
        f"{_DIST_NAME} = {{ path = {_toml_string('./' + wheel.name)} }}\n"
    )
    (src / "pyproject.toml").write_text(pyproject)
    _log("src/pyproject.toml:\n" + pyproject)


def _bundle_vars(args: argparse.Namespace) -> list[str]:
    # The app's own URL isn't known until it exists — empty on the first
    # deploy, then passed via --app-url on the second (see main()).
    app_url = args.app_url or ""
    pairs = {
        "app_name": args.app_name,
        "secret_scope": args.secret_scope,
        "oauth_client_id": args.oauth_client_id,
        "server_url": args.server_url.rstrip("/"),
        "app_url": app_url.rstrip("/"),
    }
    out: list[str] = []
    for key, value in pairs.items():
        out += ["--var", f"{key}={value}"]
    return out


def _databricks_base(args: argparse.Namespace) -> list[str]:
    cmd = ["databricks"]
    if args.profile:
        cmd += ["--profile", args.profile]
    return cmd


def _run_cli(cmd: list[str]) -> None:
    _log("$ " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=_deploy_dir())
    if result.returncode != 0:
        _fail(f"command failed ({result.returncode}): {' '.join(cmd)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-name", required=True, help="Databricks App name.")
    parser.add_argument("--profile", default=None, help="Databricks CLI profile.")
    parser.add_argument("--target", default="prod", help="Bundle target (default: prod).")
    parser.add_argument(
        "--secret-scope",
        required=True,
        help="Secret scope holding the bot's credentials (see README).",
    )
    parser.add_argument(
        "--server-url",
        required=True,
        help="Base URL of the Omnigent server app the bot talks to.",
    )
    parser.add_argument(
        "--oauth-client-id",
        required=True,
        help="Custom U2M OAuth app client id (public; passed inline, not a secret).",
    )
    parser.add_argument(
        "--app-url",
        default=None,
        help=(
            "This app's own public URL (the enrollment link base). Unknown "
            "until the app exists, so omit on the first deploy, then read it "
            "with `databricks apps get <app> -o json | jq -r .url` and pass it "
            "on a second deploy."
        ),
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Explicit PEP 440 version to stamp. Default: <base>.post<unix-ts>.",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Reuse the existing src/ wheel + pyproject — skip the wheel build.",
    )
    parser.add_argument(
        "--skip-run",
        action="store_true",
        help="Deploy the bundle but don't start the app (bundle run).",
    )
    args = parser.parse_args()

    if not args.app_url:
        _log(
            "WARNING: --app-url not set. The enrollment link needs this "
            "app's public URL, which only exists after the first deploy. Re-run "
            "with --app-url once you can read it: "
            f"databricks apps get {args.app_name} -o json | jq -r .url"
        )

    if not args.skip_build:
        deploy_version = _compute_deploy_version(_read_base_version(), args.version)
        original_pyproject = _stamp_version(deploy_version)
        try:
            wheel = _build_wheel()
            _write_app_pyproject(wheel, deploy_version)
        finally:
            # Restore the working-tree version so the deploy leaves no diff.
            (_slack_root() / "pyproject.toml").write_text(original_pyproject)
    else:
        _log("--skip-build: reusing existing src/ wheel + pyproject")
        if not list(_src_dir().glob(f"{_WHEEL_PREFIX}*.whl")):
            _fail("no wheel in src/ to reuse; run without --skip-build first")

    base = _databricks_base(args)
    variables = _bundle_vars(args)

    _run_cli([*base, "bundle", "deploy", "--target", args.target, *variables])
    if not args.skip_run:
        run_cmd = [*base, "bundle", "run", _BUNDLE_RESOURCE_KEY, "--target", args.target]
        _run_cli([*run_cmd, *variables])

    _log("done. Check the app's status/logs in the Databricks UI (Apps).")


if __name__ == "__main__":
    main()
