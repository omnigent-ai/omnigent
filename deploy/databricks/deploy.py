#!/usr/bin/env python3
"""Deploy Omnigent to a Databricks App via Databricks Asset Bundles.

End-to-end orchestrator that wraps `databricks bundle deploy` +
`databricks bundle run`. The build pieces (version stamp, wheel
build, uv lock generation, stale-wheel sweep) stay in Python; the
deploy itself is a DAB so the app's resource definition (Lakebase,
UC volume) lives declaratively in ``databricks.yml``.

Runs unchanged from a laptop or from CI. Re-runnable;
every step is idempotent.

Usage example:
    python deploy/databricks/deploy.py \\
        --app-name omnigent --profile <your-profile> \\
        --lakebase-branch projects/omnigent/branches/production \\
        --lakebase-database \\
            projects/omnigent/branches/production/databases/databricks-postgres \\
        --volume-name main.omnigent.artifacts

See ``README.md`` in the same directory for the full guide,
including first-time infrastructure setup.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

_WORKSPACE_WHEEL_LIMIT_BYTES = 10 * 1024 * 1024
_APP_REQUIRES_PYTHON = ">=3.12,<3.13"
# Public PyPI by default. Set UV_INDEX_URL to lock against a private mirror or
# proxy instead (see run_uv_lock).
_UV_DEFAULT_INDEX_URL = "https://pypi.org/simple"

# Leaving these in the env when we hand off to the CLI/SDK can
# silently route us to the wrong workspace, or upload code under the
# wrong account. The deploy must use --profile / DATABRICKS_HOST +
# DATABRICKS_CLIENT_ID explicitly.
_ENV_VARS_TO_CLEAR = (
    "DATABRICKS_TOKEN",
    "ANTHROPIC_API_KEY",
    "CODEX",
    "CLAUDE_CODE",
)

# Must match the `resources.apps.<key>` and `bundle.name` in databricks.yml.
_BUNDLE_RESOURCE_KEY = "omnigent"

_WHEEL_PREFIXES = ("omnigent-", "omnigent_client-", "omnigent_ui_sdk-")


def _log(msg: str) -> None:
    print(f"[deploy] {msg}", flush=True)


def _repo_root() -> Path:
    # deploy/databricks/deploy.py → repo root is two parents up.
    return Path(__file__).resolve().parents[2]


def _deploy_dir() -> Path:
    return Path(__file__).resolve().parent


def _src_dir() -> Path:
    return _deploy_dir() / "src"


def _pyproject_paths() -> list[Path]:
    root = _repo_root()
    return [
        root / "pyproject.toml",
        root / "sdks" / "python-client" / "pyproject.toml",
        root / "sdks" / "ui" / "pyproject.toml",
    ]


def _read_base_version() -> str:
    """Read the base version from the top-level pyproject.toml.

    The three pyprojects share the same version; we only need to read
    one. The base value is the on-disk version; deploys append a
    `.postN` suffix so pip's wheel cache treats every deploy as a
    distinct release.
    """
    text = (_repo_root() / "pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError("could not find version in pyproject.toml")
    return match.group(1)


def _compute_deploy_version(base: str, explicit: str | None) -> str:
    if explicit:
        # Caller knows what they want — let it through after a sanity check.
        if not re.match(r"^\d+(\.\d+)*(\.dev\d+|\.post\d+|[+\-][\w.]+)?$", explicit):
            raise SystemExit(f"--version {explicit!r} is not a recognizable PEP 440 version")
        return explicit
    # Strip any existing `.postN` / `.devN` suffix so we don't stack
    # them if a prior deploy left pyproject.toml dirty (or someone
    # committed the bumped value). Without this, `0.1.0.post<old>`
    # would become `0.1.0.post<old>.post<new>` which isn't valid
    # PEP 440 and fails the wheel build.
    base = re.sub(r"(\.post\d+|\.dev\d+)+$", "", base)
    # Post-release, not dev: pip treats `.dev` as a pre-release and
    # ignores it when resolving `>=` constraints, so a deploy that
    # bumps via `.dev` clashes with `omnigent-ui-sdk` declaring
    # `omnigent-client>=0.1.0`. `.post` is a final release and
    # sorts strictly above the base.
    return f"{base}.post{int(time.time())}"


def set_version_in_pyproject(path: Path, new_version: str) -> str:
    """Rewrite the version line and lockstep sibling pins in a pyproject.

    The release process pins the sibling SDK packages in lockstep
    (e.g. ``"omnigent-client==0.1.0rc2"``). Stamping only the
    ``version = "..."`` line would leave those exact pins pointing at
    the unstamped base version, which no built wheel carries — so the
    app-level ``uv lock`` becomes unsatisfiable. Rewrite both.

    :param path: Pyproject file to rewrite, e.g.
        ``<repo>/sdks/ui/pyproject.toml``.
    :param new_version: Stamped deploy version, e.g. ``"0.1.0rc2.post123"``.
    :returns: The original file text, for restore after the build.
    """
    original = path.read_text()
    updated, count = re.subn(
        r'(?m)^version\s*=\s*"[^"]+"',
        f'version = "{new_version}"',
        original,
        count=1,
    )
    if count != 1:
        raise RuntimeError(f"could not rewrite version in {path}")
    # The lockstep graph is circular: omnigent pins both SDKs, the
    # client pins omnigent back, and the ui-sdk pins the client. All
    # three names must be stamped or the resolver still dead-ends.
    updated = re.sub(
        r'"(omnigent(?:-client|-ui-sdk)?)==[^"]+"',
        rf'"\1=={new_version}"',
        updated,
    )
    path.write_text(updated)
    return original


def _stamp_versions(new_version: str) -> dict[Path, str]:
    """Stamp `new_version` into all three pyprojects. Returns originals for restore."""
    backups: dict[Path, str] = {}
    for path in _pyproject_paths():
        backups[path] = set_version_in_pyproject(path, new_version)
    return backups


def _restore_versions(backups: dict[Path, str]) -> None:
    for path, original in backups.items():
        path.write_text(original)


def _clean_build_artifacts() -> None:
    """Remove stale build outputs.

    Old Vite bundles in ``omnigent/server/static/web-ui/``
    accumulate uniquely-hashed JS chunk filenames between builds.
    Without this sweep, orphan files get bundled into the main wheel
    and push it over the 10 MB Workspace upload limit.
    """
    root = _repo_root()
    targets = [
        root / "omnigent" / "server" / "static" / "web-ui",
        root / "dist",
        root / "build",
        root / "omnigent.egg-info",
    ]
    for target in targets:
        if target.exists():
            _log(f"cleaning {target.relative_to(root)}")
            shutil.rmtree(target)


def _build_wheels(skip_web_ui: bool) -> list[Path]:
    """Invoke build.sh and return the resulting wheel paths."""
    root = _repo_root()
    build_sh = _deploy_dir() / "build.sh"
    env = os.environ.copy()
    if skip_web_ui:
        env["SKIP_WEB_UI"] = "1"
    _log(f"$ {build_sh}" + (" (SKIP_WEB_UI=1)" if skip_web_ui else ""))
    subprocess.run([str(build_sh)], cwd=root, env=env, check=True)
    wheels = sorted((root / "dist").glob("*.whl"))
    if not wheels:
        raise RuntimeError("build.sh produced no wheels")
    return wheels


@dataclass(frozen=True)
class _ClassifiedWheels:
    """Result of sorting built wheels by size for upload routing.

    :param main: The top-level ``omnigent`` wheel — always uploaded
        with the ``[databricks]`` extra.
    :param small: Wheels ≤ 10 MB. Uploaded into the bundle's
        ``source_code_path`` and referenced by relative path.
    :param oversize: Wheels > 10 MB. These cannot be used by the
        uv-based app payload because ``uv lock`` validates local path
        sources before the bundle is synced.
    """

    main: Path
    small: list[Path]
    oversize: list[Path]


def _classify_wheels(wheels: Iterable[Path]) -> _ClassifiedWheels:
    main_wheel = next(w for w in wheels if w.name.startswith("omnigent-"))
    small: list[Path] = []
    oversize: list[Path] = []
    for wheel in wheels:
        if wheel.stat().st_size <= _WORKSPACE_WHEEL_LIMIT_BYTES:
            small.append(wheel)
        else:
            oversize.append(wheel)
    return _ClassifiedWheels(main=main_wheel, small=small, oversize=oversize)


def _wheel_version(wheel: Path, prefix: str) -> str:
    """Extract a deploy version from an Omnigent wheel filename.

    :param wheel: Built wheel path, e.g.
        ``dist/omnigent-0.1.0.post123-py3-none-any.whl``.
    :param prefix: Expected wheel filename prefix, e.g. ``"omnigent-"``.
    :returns: Version embedded in the wheel filename, e.g.
        ``"0.1.0.post123"``.
    :raises RuntimeError: If the wheel filename does not match the
        deploy script's expected wheel naming convention.
    """
    pattern = rf"^{re.escape(prefix)}(?P<version>.+)-py3-none-any\.whl$"
    match = re.match(pattern, wheel.name)
    if not match:
        raise RuntimeError(f"unexpected wheel name for {prefix}: {wheel.name}")
    return match.group("version")


def _derive_deploy_version_from_wheels(wheels: list[Path]) -> str:
    """Derive the deploy version from reused ``dist/`` wheels.

    :param wheels: Wheel files from ``dist/``.
    :returns: Shared wheel version, e.g. ``"0.1.0.post123"``.
    :raises RuntimeError: If any required wheel is missing or the
        wheel versions do not match.
    """
    expected_prefixes = ("omnigent-", "omnigent_client-", "omnigent_ui_sdk-")
    versions = []
    for prefix in expected_prefixes:
        matching = [wheel for wheel in wheels if wheel.name.startswith(prefix)]
        if len(matching) != 1:
            raise RuntimeError(f"expected exactly one {prefix} wheel, found {len(matching)}")
        versions.append(_wheel_version(matching[0], prefix))
    if len(set(versions)) != 1:
        raise RuntimeError(f"wheel versions do not match: {versions}")
    return versions[0]


def _sweep_local_src_wheels(keep: set[str]) -> None:
    """Delete Omnigent wheels from src/ whose filename is not in `keep`.

    Old deploys accumulate wheels here. Databricks Apps installs the
    source directory as a project, so stale wheels can keep local path
    sources or lockfile entries alive if we leave them around. Trim to
    exactly the wheels we just built.
    """
    src = _src_dir()
    if not src.exists():
        return
    for entry in src.iterdir():
        if not entry.is_file() or entry.suffix != ".whl":
            continue
        if not any(entry.name.startswith(p) for p in _WHEEL_PREFIXES):
            continue
        if entry.name in keep:
            continue
        _log(f"removing stale local wheel {entry.relative_to(_repo_root())}")
        entry.unlink()


def _toml_string(value: str) -> str:
    """Return ``value`` encoded as a TOML basic string.

    :param value: String to encode for generated TOML, e.g.
        ``"./omnigent-0.1.0-py3-none-any.whl"``.
    :returns: TOML string literal with quotes and backslashes escaped.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _wheel_source_path(wheel: Path) -> str:
    """Return the path Databricks Apps should use for a wheel source.

    :param wheel: Built wheel path, e.g.
        ``dist/omnigent-0.1.0-py3-none-any.whl``.
    :returns: Relative source path for bundled wheels.
    :raises SystemExit: If ``wheel`` is oversize.
    """
    if wheel.stat().st_size <= _WORKSPACE_WHEEL_LIMIT_BYTES:
        return f"./{wheel.name}"
    raise SystemExit(
        f"wheel {wheel.name} is over 10 MB; uv-based Databricks Apps "
        "deploys require all wheels in the app source snapshot"
    )


def _uv_source_lines(
    main_wheel: Path,
    small_wheels: list[Path],
    oversize_wheels: list[Path],
) -> list[str]:
    """Build ``[tool.uv.sources]`` lines for the three deploy wheels.

    :param main_wheel: Top-level ``omnigent`` wheel path, e.g.
        ``dist/omnigent-0.1.0-py3-none-any.whl``.
    :param small_wheels: Wheels copied into the app source directory.
    :param oversize_wheels: Wheels too large for the app source directory.
    :returns: TOML lines mapping package names to wheel paths.
    """
    wheels = {wheel.name: wheel for wheel in [*small_wheels, *oversize_wheels]}
    if main_wheel.name not in wheels:
        raise RuntimeError(f"main wheel {main_wheel.name} was not classified for deployment")
    source_lines = []
    for package_name, wheel_prefix in (
        ("omnigent", "omnigent-"),
        ("omnigent-client", "omnigent_client-"),
        ("omnigent-ui-sdk", "omnigent_ui_sdk-"),
    ):
        wheel = next(wheel for name, wheel in wheels.items() if name.startswith(wheel_prefix))
        source = _wheel_source_path(wheel)
        source_lines.append(f"{package_name} = {{ path = {_toml_string(source)} }}")
    return source_lines


def build_uv_pyproject(
    main_wheel: Path,
    small_wheels: list[Path],
    oversize_wheels: list[Path],
    deploy_version: str,
) -> str:
    """Compose the app-level ``pyproject.toml`` for Databricks Apps.

    :param main_wheel: Top-level ``omnigent`` wheel path, e.g.
        ``dist/omnigent-0.1.0-py3-none-any.whl``.
    :param small_wheels: Wheels copied into the app source directory.
    :param oversize_wheels: Wheels too large for the app source directory.
    :param deploy_version: Version stamped into the wheels, e.g.
        ``"0.1.0.post123"``.
    :returns: Complete TOML text for ``src/pyproject.toml``.
    """
    source_lines = _uv_source_lines(main_wheel, small_wheels, oversize_wheels)
    dependencies = [
        f'"omnigent[databricks]=={deploy_version}"',
        f'"omnigent-client=={deploy_version}"',
        f'"omnigent-ui-sdk=={deploy_version}"',
    ]
    return (
        "[project]\n"
        'name = "omnigent-databricks-app"\n'
        'version = "0.0.0"\n'
        f"requires-python = {_toml_string(_APP_REQUIRES_PYTHON)}\n"
        "dependencies = [\n"
        + "".join(f"  {dependency},\n" for dependency in dependencies)
        + "]\n\n"
        "[tool.uv.sources]\n" + "\n".join(source_lines) + "\n"
    )


def run_uv_lock(src: Path) -> None:
    """Generate ``uv.lock`` for the Databricks Apps source directory.

    :param src: App source directory containing ``pyproject.toml``,
        e.g. ``deploy/databricks/src``.
    """
    # Honor a caller-supplied UV_INDEX_URL (e.g. a private mirror or proxy);
    # otherwise default to public PyPI. UV_INDEX / UV_DEFAULT_INDEX are dropped
    # so a stray value in the shell can't shadow the index we lock against.
    index_url = os.environ.get("UV_INDEX_URL") or _UV_DEFAULT_INDEX_URL
    env = os.environ.copy()
    env.pop("UV_INDEX", None)
    env.pop("UV_DEFAULT_INDEX", None)
    env["UV_INDEX_URL"] = index_url
    _log(f"uv lock --python 3.12 --index-url {index_url}")
    subprocess.run(
        ["uv", "lock", "--python", "3.12", "--index-url", index_url],
        cwd=src,
        env=env,
        check=True,
    )


def write_uv_dependency_files(
    src: Path,
    main_wheel: Path,
    small_wheels: list[Path],
    oversize_wheels: list[Path],
    deploy_version: str,
) -> None:
    """Write the uv dependency files Databricks Apps should install.

    :param src: App source directory, e.g. ``deploy/databricks/src``.
    :param main_wheel: Top-level ``omnigent`` wheel path, e.g.
        ``dist/omnigent-0.1.0-py3-none-any.whl``.
    :param small_wheels: Wheels copied into ``src``.
    :param oversize_wheels: Wheels too large for ``src``.
    :param deploy_version: Version stamped into the wheels, e.g.
        ``"0.1.0.post123"``.
    """
    requirements = src / "requirements.txt"
    if requirements.exists():
        _log(f"removing {requirements}; Databricks Apps must use uv")
        requirements.unlink()

    pyproject = build_uv_pyproject(
        main_wheel,
        small_wheels,
        oversize_wheels,
        deploy_version,
    )
    (src / "pyproject.toml").write_text(pyproject)
    _log("src/pyproject.toml:\n" + pyproject)
    run_uv_lock(src)


def _smoke_check(wc: WorkspaceClient, app_url: str) -> None:
    """Poll /health on the running app and fail if it never returns 200.

    ``databricks bundle run`` returns as soon as the app start is
    signalled, but uvicorn takes a few extra seconds to bind. Retry
    up to a minute to ride out the warm-up; surface the most recent
    error if it never goes green.
    """
    import urllib.error
    import urllib.request

    token = wc.config.authenticate()["Authorization"].removeprefix("Bearer ").strip()
    url = f"{app_url.rstrip('/')}/health"

    last_err: str = ""
    for attempt in range(12):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode()
                if resp.status == 200:
                    _log(f"/health ok: {body!r}")
                    return
                last_err = f"HTTP {resp.status}: {body!r}"
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}: {exc.reason}"
        except urllib.error.URLError as exc:
            last_err = f"URLError: {exc.reason}"
        if attempt < 11:
            time.sleep(5)
    raise RuntimeError(f"/health did not return 200 within 60s; last: {last_err}")


def _assert_clean_tree(skip: bool) -> None:
    """Refuse to deploy if the working tree has uncommitted changes or
    HEAD is not at origin/main.

    Pass ``--allow-dirty`` to override. Reason: deploys are intended
    to come from a known commit on main, so the deployed app code is
    reproducible from git history. A dirty tree means whatever we
    deploy is not reachable from any commit, which is the cause of
    nearly every "wait, what's actually live?" debugging session.
    """
    root = _repo_root()
    if skip:
        _log("--allow-dirty: skipping clean-tree assertion")
        return
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise SystemExit(
            "working tree has uncommitted changes:\n"
            + status
            + "\ncommit or stash, or pass --allow-dirty to override."
        )
    subprocess.run(["git", "fetch", "origin", "main", "--quiet"], cwd=root, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    main = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != main:
        raise SystemExit(
            f"HEAD ({head}) is not at origin/main ({main}); "
            f"rebase or pass --allow-dirty to override."
        )
    _log(f"clean tree at origin/main {head[:12]}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app-name",
        required=True,
        help="Databricks App name, e.g. 'omnigent'.",
    )
    parser.add_argument(
        "--lakebase-branch",
        required=True,
        help=("Full Lakebase branch resource path, e.g. 'projects/omnigent/branches/production'."),
    )
    parser.add_argument(
        "--lakebase-database",
        required=True,
        help=(
            "Full Lakebase database resource path, e.g. "
            "'projects/omnigent/branches/production/databases/databricks-postgres'."
        ),
    )
    parser.add_argument(
        "--volume-name",
        required=True,
        help=(
            "UC Volume full name (catalog.schema.volume) for artifact storage, "
            "e.g. 'main.omnigent.artifacts'."
        ),
    )
    parser.add_argument(
        "--compute-size",
        default="LARGE",
        choices=["SMALL", "MEDIUM", "LARGE"],
        help="App compute size. Pinned via databricks.yml so it doesn't drift.",
    )
    parser.add_argument(
        "--otel-table-schema",
        default="main.omnigent_logs",
        help=(
            "UC schema (catalog.schema) holding the OTel destination tables. "
            "The Databricks Apps platform writes logs/metrics/spans to "
            "<schema>.otel_{logs,metrics,spans}."
        ),
    )
    parser.add_argument(
        "--target",
        default="prod",
        help=(
            "DAB target from databricks.yml selecting the destination "
            "workspace, e.g. 'prod'. The authenticating identity "
            "(--profile or env) must belong to the target's workspace."
        ),
    )
    parser.add_argument(
        "--profile",
        default=None,
        help=(
            "Databricks CLI profile to authenticate with. Omit when running "
            "with env-based auth (DATABRICKS_HOST + DATABRICKS_CLIENT_ID + "
            "DATABRICKS_CLIENT_SECRET)."
        ),
    )
    parser.add_argument(
        "--version",
        default=None,
        help=(
            "Explicit PEP 440 version to stamp into pyprojects for this "
            "deploy. Default: <base-version>.post<unix-ts>."
        ),
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Reuse existing dist/ wheels — skip the npm + uv build.",
    )
    parser.add_argument(
        "--skip-web-ui",
        action="store_true",
        help="Build without the SPA (API-only deploy).",
    )
    parser.add_argument(
        "--app-url",
        default=None,
        help=(
            "Override the smoke-check URL. By default the script reads "
            "the URL from the App resource returned by the SDK."
        ),
    )
    parser.add_argument(
        "--no-smoke-check",
        action="store_true",
        help="Skip the post-deploy /health check.",
    )
    parser.add_argument(
        "--keep-version-bump",
        action="store_true",
        help=(
            "Don't restore pyproject.toml files after build. Useful "
            "when you want the bumped version to land in a commit "
            "(e.g. CI auto-tagged release builds)."
        ),
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "Skip the assertion that the working tree is clean and at "
            "origin/main. Deploys are normally required to be from a "
            "known commit on main."
        ),
    )
    return parser.parse_args()


def _clear_env_vars() -> None:
    for name in _ENV_VARS_TO_CLEAR:
        if name in os.environ:
            _log(f"unsetting {name} to avoid leaking into the SDK")
            del os.environ[name]


def _ensure_bound(args: argparse.Namespace) -> None:
    """Bind the bundle to the named app if it exists but is unbound.

    If the app already exists in the workspace and the bundle has no
    Terraform state for it (always true on a fresh checkout),
    ``bundle deploy`` fails with "An app with the same name already
    exists". The fix is ``bundle deployment bind``, which adopts the
    existing app into the bundle's state.

    There's no clean way to ask "is the bundle already bound to this
    app" — ``bundle summary`` reflects what's *declared* in the YAML,
    not what's tracked by Terraform. So we just always try to bind
    when the app exists and treat "already managed by Terraform" as
    success.
    """
    # Late-import so --help works without the SDK.
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.errors.platform import NotFound

    wc = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    try:
        wc.apps.get(name=args.app_name)
    except NotFound:
        _log(f"app {args.app_name!r} not found; bundle deploy will create it")
        return

    _log(f"binding bundle resource {_BUNDLE_RESOURCE_KEY!r} → app {args.app_name!r}")
    result = subprocess.run(
        [
            "databricks",
            "bundle",
            "deployment",
            "bind",
            _BUNDLE_RESOURCE_KEY,
            args.app_name,
            "--target",
            args.target,
            "--auto-approve",
            *_profile_arg(args),
            *_bundle_vars(args),
        ],
        cwd=_deploy_dir(),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return
    combined = "".join(stream for stream in (result.stdout, result.stderr) if stream)
    if "already managed" in combined.lower() or "already bound" in combined.lower():
        _log("bundle already bound to this app; continuing")
        return
    sys.stderr.write(combined)
    raise SystemExit(f"bundle deployment bind failed (exit {result.returncode})")


def _ensure_compute_size(
    wc: WorkspaceClient,
    app_name: str,
    desired: str,
) -> None:
    """Resize the app to `desired` if it's not already there.

    The bundle's Terraform databricks_app resource can't update
    compute_size on an existing app (the old apps update API rejects
    it). The newer ``apps.create_update`` endpoint can. Run that here
    so the subsequent bundle deploy sees no diff and doesn't error.
    """
    from databricks.sdk.errors.platform import NotFound
    from databricks.sdk.service.apps import App, ComputeSize

    try:
        current = wc.apps.get(name=app_name)
    except NotFound:
        _log(f"app {app_name!r} not found; bundle deploy will create at {desired}")
        return

    current_value = current.compute_size.value if current.compute_size else None
    if current_value == desired:
        _log(f"compute_size already {desired}; skipping resize")
        return

    _log(f"resizing app {app_name!r}: {current_value} → {desired}")
    wc.apps.create_update_and_wait(
        app_name=app_name,
        update_mask="compute_size",
        app=App(name=app_name, compute_size=ComputeSize(desired)),
    )


def _bundle_vars(args: argparse.Namespace) -> list[str]:
    """CLI args to pass to `databricks bundle` as --var pairs."""
    return [
        "--var",
        f"app_name={args.app_name}",
        "--var",
        f"lakebase_branch={args.lakebase_branch}",
        "--var",
        f"lakebase_database={args.lakebase_database}",
        "--var",
        f"volume_name={args.volume_name}",
        "--var",
        f"otel_table_schema={args.otel_table_schema}",
    ]


def _profile_arg(args: argparse.Namespace) -> list[str]:
    return ["--profile", args.profile] if args.profile else []


def _ensure_app_sp_uc_traversal(
    args: argparse.Namespace,
    app_sp: str | None,
) -> None:
    """Grant USE_CATALOG + USE_SCHEMA to the app SP on the volume's parents.

    Apps' ``uc_securable`` only grants the leaf (WRITE_VOLUME); the
    SP can boot but 403s on first volume read if the parent catalog
    doesn't grant USE to ``account users``. Idempotent.
    """
    if not app_sp:
        _log("app SP not resolved yet; skipping UC traversal grants")
        return

    parts = args.volume_name.split(".")
    if len(parts) != 3:
        raise SystemExit(f"--volume-name {args.volume_name!r} must be catalog.schema.volume")
    catalog, schema_only, _ = parts
    schema_fqn = f"{catalog}.{schema_only}"

    import json as _json

    for kind, fqn, priv in (
        ("catalog", catalog, "USE_CATALOG"),
        ("schema", schema_fqn, "USE_SCHEMA"),
    ):
        _log(f"granting {priv} on {kind} {fqn} → app SP {app_sp}")
        payload = _json.dumps({"changes": [{"principal": app_sp, "add": [priv]}]})
        subprocess.run(
            [
                "databricks",
                "grants",
                "update",
                kind,
                fqn,
                *_profile_arg(args),
                "--json",
                payload,
            ],
            check=True,
            capture_output=True,
            text=True,
        )


def main() -> int:
    args = _parse_args()
    _clear_env_vars()
    _assert_clean_tree(skip=args.allow_dirty)

    base_version = _read_base_version()
    deploy_version = _compute_deploy_version(base_version, args.version)
    _log(f"deploy version: {deploy_version} (base: {base_version})")

    backups: dict[Path, str] = {}
    try:
        if not args.skip_build:
            _clean_build_artifacts()
            backups = _stamp_versions(deploy_version)
            wheels = _build_wheels(skip_web_ui=args.skip_web_ui)
        else:
            dist = _repo_root() / "dist"
            wheels = sorted(dist.glob("*.whl"))
            if not wheels:
                raise SystemExit("--skip-build was set but dist/ has no wheels to redeploy")
            wheel_version = _derive_deploy_version_from_wheels(wheels)
            if args.version and args.version != wheel_version:
                raise SystemExit(
                    f"--version {args.version!r} does not match reused wheel "
                    f"version {wheel_version!r}"
                )
            deploy_version = wheel_version
            _log(f"deploy version from reused wheels: {deploy_version}")
            _log(f"reusing wheels: {[w.name for w in wheels]}")
    finally:
        if backups and not args.keep_version_bump:
            _restore_versions(backups)

    classified = _classify_wheels(wheels)
    for wheel in wheels:
        size_mb = wheel.stat().st_size / 1024 / 1024
        _log(f"  {wheel.name}  {size_mb:.2f} MB")
    if classified.oversize:
        raise SystemExit(
            "uv-based Databricks Apps deploys require all Omnigent wheels "
            "to fit in the app source snapshot. Rebuild with --skip-web-ui "
            "or reduce wheel size; UC Volume wheel paths are not used "
            "because uv lock validates path sources locally."
        )

    # Late-import the SDK so `--help` works without it installed.
    from databricks.sdk import WorkspaceClient

    wc = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()

    # 1) Prep the bundle's source_code_path (src/) — sweep stale
    # wheels locally, then copy the new small wheels in.
    src = _src_dir()
    src.mkdir(parents=True, exist_ok=True)
    _sweep_local_src_wheels(keep={w.name for w in classified.small})
    for wheel in classified.small:
        dest = src / wheel.name
        _log(f"copy {wheel.name} → {dest.relative_to(_repo_root())}")
        shutil.copy2(wheel, dest)

    # 2) Generate pyproject.toml + uv.lock. Remove requirements.txt
    # first because Databricks Apps gives it precedence over uv.
    write_uv_dependency_files(
        src,
        classified.main,
        classified.small,
        classified.oversize,
        deploy_version,
    )

    # 4) Bind the bundle to the existing app (if any).
    _ensure_bound(args)

    # 4a) Reconcile compute_size out-of-band. Terraform's databricks_app
    # update path doesn't support compute_size changes ("not supported
    # in this update API"). The new SDK apps.create_update endpoint
    # does — call it ourselves so the subsequent bundle deploy sees no
    # diff. Skipped when already matching.
    _ensure_compute_size(wc, args.app_name, args.compute_size)

    # 5) databricks bundle deploy --target <target> (syncs src/ to the
    # bundle workspace folder and creates/updates the app resource).
    _log(f"databricks bundle deploy --target {args.target}")
    subprocess.run(
        [
            "databricks",
            "bundle",
            "deploy",
            "--target",
            args.target,
            *_profile_arg(args),
            *_bundle_vars(args),
        ],
        cwd=_deploy_dir(),
        check=True,
    )

    # 5) databricks bundle run <key> --target <target> (starts/restarts
    # the app with the just-deployed source).
    _log(f"databricks bundle run {_BUNDLE_RESOURCE_KEY} --target {args.target}")
    subprocess.run(
        [
            "databricks",
            "bundle",
            "run",
            _BUNDLE_RESOURCE_KEY,
            "--target",
            args.target,
            *_profile_arg(args),
            *_bundle_vars(args),
        ],
        cwd=_deploy_dir(),
        check=True,
    )

    # 6) Resolve URL + smoke-check.
    app = wc.apps.get(name=args.app_name)
    app_url = args.app_url or app.url

    # 7) Grant the app SP USE_CATALOG + USE_SCHEMA on the volume's
    # parents (Apps' uc_securable resource only grants the leaf).
    _ensure_app_sp_uc_traversal(args, app.service_principal_client_id)

    if not args.no_smoke_check:
        if not app_url:
            raise SystemExit("no app URL available for smoke check")
        _smoke_check(wc, app_url)
    _log(f"done. app: {app_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
