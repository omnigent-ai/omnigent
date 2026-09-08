"""The Databricks Apps deploy ships the SPA outside the wheel.

Databricks Apps uploads the app source directory as Workspace files, and the
Workspace import API rejects any single file over 10 MB. The built SPA is ~25 MB
of assets, which takes the main wheel over that cap and fails the deploy before
anything is uploaded. So ``build.sh`` moves the SPA out of the wheel's package
data and the deploy ships it as one archive in ``src/web-ui.tar.gz``, which
``src/app.py`` extracts and points the server at via ``OMNIGENT_WEB_UI_DIST``.

These tests pin each link in that chain, including the env var itself — the
server reads it at import time, so it is checked in a subprocess.
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY_DIR = _ROOT / "deploy" / "databricks"
_DEPLOY_PY = _DEPLOY_DIR / "deploy.py"
_BUILD_SH = _DEPLOY_DIR / "build.sh"
_APP_PY = _DEPLOY_DIR / "src" / "app.py"


@pytest.fixture(scope="module")
def deploy_mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_databricks_deploy_webui", _DEPLOY_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_spa(root: Path, *, asset_bytes: int = 32) -> Path:
    spa = root / "web-ui"
    (spa / "assets").mkdir(parents=True)
    (spa / "index.html").write_text("<!doctype html><title>omnigent</title>")
    (spa / "assets" / "index-abc123.js").write_bytes(b"x" * asset_bytes)
    return spa


def _make_spa_archive(root: Path, *, asset_bytes: int = 32) -> Path:
    spa = _make_spa(root, asset_bytes=asset_bytes)
    archive = root / "web-ui.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in spa.rglob("*"):
            tar.add(path, arcname=path.relative_to(spa))
    shutil.rmtree(spa)
    return archive


def _load_archive_extractor():
    """Load the side-effect-free archive helper without starting the app."""
    helper = _DEPLOY_DIR / "src" / "web_ui_archive.py"
    spec = importlib.util.spec_from_file_location("web_ui_archive_test", helper)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.extract_web_ui_archive, module


def test_server_honours_web_ui_dist_env(tmp_path: Path) -> None:
    """The whole scheme hinges on OMNIGENT_WEB_UI_DIST being respected."""
    spa = _make_spa(tmp_path)
    code = "import omnigent.server.app as m; print(m._WEB_UI_DIST)"
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        env={"PATH": "/usr/bin:/bin", "OMNIGENT_WEB_UI_DIST": str(spa)},
        check=True,
    )
    assert out.stdout.strip() == str(spa)


def test_app_py_extracts_web_ui_before_importing_server() -> None:
    """The extracted path is read at import time, so setup must come first."""
    source = _APP_PY.read_text()
    prepare_at = source.rindex("_prepare_web_ui()")
    import_at = source.index("from omnigent.server.app import create_app")
    assert prepare_at < import_at, "web UI setup is too late to take effect"
    assert 'archive = here / "web-ui.tar.gz"' in source
    assert "from web_ui_archive import extract_web_ui_archive" in source
    assert 'loose = here / "web-ui"' in source


def test_app_extractor_extracts_normal_archive(tmp_path: Path) -> None:
    extractor, _ = _load_archive_extractor()
    archive = _make_spa_archive(tmp_path)
    destination = tmp_path / "extracted"
    destination.mkdir()

    extractor(archive, destination)

    assert (destination / "index.html").is_file()
    assert (destination / "assets" / "index-abc123.js").is_file()


def test_app_extractor_rejects_oversize_expansion_before_writing(tmp_path: Path) -> None:
    extractor, module = _load_archive_extractor()
    archive = tmp_path / "web-ui.tar.gz"
    payload = b"x" * (module.MAX_MEMBER_BYTES + 1)
    info = tarfile.TarInfo("assets/bomb.js")
    info.size = len(payload)
    with tarfile.open(archive, "w:gz") as tar:
        tar.addfile(info, io.BytesIO(payload))
    destination = tmp_path / "extracted"
    destination.mkdir()

    with pytest.raises(ValueError, match="10 MB limit"):
        extractor(archive, destination)
    assert not any(destination.iterdir())


def test_app_extractor_rejects_aggregate_expansion_before_writing(tmp_path: Path) -> None:
    extractor, module = _load_archive_extractor()
    archive = tmp_path / "web-ui.tar.gz"
    member_bytes = 1024 * 1024
    member_count = module.MAX_EXTRACTED_BYTES // member_bytes + 1
    with tarfile.open(archive, "w:gz") as tar:
        for index in range(member_count):
            info = tarfile.TarInfo(f"assets/chunk-{index}.js")
            info.size = member_bytes
            tar.addfile(info, io.BytesIO(b"x" * member_bytes))
    destination = tmp_path / "extracted"
    destination.mkdir()

    with pytest.raises(ValueError, match="web UI size limit"):
        extractor(archive, destination)
    assert not any(destination.iterdir())


def test_app_extractor_rejects_too_many_members_before_writing(tmp_path: Path) -> None:
    extractor, module = _load_archive_extractor()
    archive = tmp_path / "web-ui.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for index in range(module.MAX_MEMBERS + 1):
            tar.addfile(tarfile.TarInfo(f"assets/chunk-{index}.js"))
    destination = tmp_path / "extracted"
    destination.mkdir()

    with pytest.raises(ValueError, match="too many members"):
        extractor(archive, destination)
    assert not any(destination.iterdir())


def test_app_extractor_rejects_traversal(tmp_path: Path) -> None:
    extractor, _ = _load_archive_extractor()
    archive = tmp_path / "web-ui.tar.gz"
    info = tarfile.TarInfo("../escaped.js")
    info.size = 1
    with tarfile.open(archive, "w:gz") as tar:
        tar.addfile(info, io.BytesIO(b"x"))
    destination = tmp_path / "extracted"
    destination.mkdir()

    with pytest.raises(tarfile.FilterError):
        extractor(archive, destination)
    assert not (tmp_path / "escaped.js").exists()


def test_build_sh_archives_spa_out_of_the_wheel() -> None:
    source = _BUILD_SH.read_text()
    assert 'tar -czf "${REPO_ROOT}/dist/web-ui.tar.gz"' in source
    # The archive has to be created while the SPA build is in scope, before uv build.
    assert source.index("EXTERNALIZE_WEB_UI") < source.index("uv build --wheel --out-dir dist/ .")


def test_build_sh_opts_the_backend_out_of_rebuilding_the_spa() -> None:
    """setup.py rebuilds the SPA into the package when the bundle is missing.

    Without the opt-out it puts the assets straight back into the wheel, undoing
    the archive, so the externalization has to happen before any wheel build.
    """
    source = _BUILD_SH.read_text()
    assert "export OMNIGENT_SKIP_WEB_UI=true" in source
    assert source.index("export OMNIGENT_SKIP_WEB_UI=true") < source.index(
        "uv build --wheel --out-dir dist/ sdks/python-client/"
    )


def test_build_sh_archives_the_spa_and_opts_out_when_run_for_real(tmp_path: Path) -> None:
    """Execute the real script with fake ``pnpm``/``uv`` and check both effects.

    Text assertions cannot show that the archive and the opt-out happen
    together on the full-UI path, which is the whole invariant: the SPA ends up
    outside the wheel *and* the wheel hook does not put it back.
    """
    repo = tmp_path / "repo"
    script = repo / "deploy" / "databricks" / "build.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(_BUILD_SH, script)

    command_log = tmp_path / "commands.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # `pnpm --filter web run build` is what produces the SPA bundle.
    _write_executable(
        fake_bin / "pnpm",
        "#!/usr/bin/env bash\n"
        'printf "pnpm|%s\\n" "$*" >> "$COMMAND_LOG"\n'
        'if [[ "$*" == *"run build"* ]]; then\n'
        "  mkdir -p omnigent/server/static/web-ui/assets\n"
        '  echo "<html></html>" > omnigent/server/static/web-ui/index.html\n'
        '  echo "chunk" > omnigent/server/static/web-ui/assets/index-abc.js\n'
        "fi\n",
    )
    _write_executable(
        fake_bin / "uv",
        "#!/usr/bin/env bash\n"
        'printf "uv|%s|%s\\n" "${OMNIGENT_SKIP_WEB_UI-<unset>}" "$*" >> "$COMMAND_LOG"\n'
        "mkdir -p dist\n"
        "touch dist/fake.whl\n",
    )

    env = os.environ.copy()
    env.update(
        {
            "COMMAND_LOG": str(command_log),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "EXTERNALIZE_WEB_UI": "1",
        }
    )
    env.pop("OMNIGENT_SKIP_WEB_UI", None)
    env.pop("SKIP_WEB_UI", None)

    subprocess.run(["bash", str(script)], cwd=repo, env=env, check=True)

    # The SPA moved out of the package tree into one archive.
    archive = repo / "dist" / "web-ui.tar.gz"
    assert archive.is_file()
    with tarfile.open(archive, "r:gz") as tar:
        names = {name.lstrip("./") for name in tar.getnames()}
        assert {"assets/index-abc.js", "index.html"} <= names
    assert not (repo / "omnigent" / "server" / "static" / "web-ui").exists()

    # And every wheel build saw the opt-out, so setup.py cannot rebuild it back in.
    uv_calls = [line for line in command_log.read_text().splitlines() if line.startswith("uv|")]
    assert len(uv_calls) == 3
    assert all(call.split("|", 2)[1] == "true" for call in uv_calls), uv_calls


def test_build_sh_uses_a_fixed_archive_destination() -> None:
    """The archive path is fixed, so no caller-controlled path is destructive."""
    source = _BUILD_SH.read_text()
    assert "dist/web-ui.tar.gz" in source
    assert 'rm -rf "${REPO_ROOT}/dist/web-ui" "${REPO_ROOT}/dist/web-ui.tar.gz"' in source
    assert "WEB_UI_OUT_NAME" not in source


def test_build_sh_leaves_the_spa_in_the_wheel_without_externalize(tmp_path: Path) -> None:
    """Externalization is opt-in: an ordinary build is unchanged.

    Without ``EXTERNALIZE_WEB_UI`` the SPA stays in the package tree and the
    wheel hook is not opted out, so a plain ``build.sh`` behaves as on main.
    """
    repo = tmp_path / "repo"
    script = repo / "deploy" / "databricks" / "build.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(_BUILD_SH, script)

    command_log = tmp_path / "commands.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "pnpm",
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"run build"* ]]; then\n'
        "  mkdir -p omnigent/server/static/web-ui\n"
        '  echo "<html></html>" > omnigent/server/static/web-ui/index.html\n'
        "fi\n",
    )
    _write_executable(
        fake_bin / "uv",
        "#!/usr/bin/env bash\n"
        'printf "uv|%s\\n" "${OMNIGENT_SKIP_WEB_UI-<unset>}" >> "$COMMAND_LOG"\n'
        "mkdir -p dist\n"
        "touch dist/fake.whl\n",
    )

    env = os.environ.copy()
    env.update({"COMMAND_LOG": str(command_log), "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}"})
    env.pop("OMNIGENT_SKIP_WEB_UI", None)
    env.pop("SKIP_WEB_UI", None)
    env.pop("EXTERNALIZE_WEB_UI", None)

    subprocess.run(["bash", str(script)], cwd=repo, env=env, check=True)

    assert (repo / "omnigent" / "server" / "static" / "web-ui" / "index.html").is_file()
    uv_calls = [line for line in command_log.read_text().splitlines() if line.startswith("uv|")]
    assert uv_calls == ["uv|<unset>"] * 3, uv_calls


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def test_setup_py_honours_the_web_ui_opt_out() -> None:
    """The opt-out build.sh relies on must keep working."""
    source = (_ROOT / "setup.py").read_text()
    assert 'os.environ.get("OMNIGENT_SKIP_WEB_UI") == "true"' in source


def test_build_wheels_requests_the_spa_archive_outside_the_wheel(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, str] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> None:
        captured.update(kwargs["env"])  # type: ignore[arg-type]
        (tmp_path / "dist").mkdir(exist_ok=True)
        (tmp_path / "dist" / "omnigent-1.0-py3-none-any.whl").write_bytes(b"w")

    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)

    deploy_mod._build_wheels(skip_web_ui=False)
    assert captured["EXTERNALIZE_WEB_UI"] == "1"
    assert "SKIP_WEB_UI" not in captured
    assert "OMNIGENT_ENABLE_CANVAS" not in captured

    captured.clear()
    deploy_mod._build_wheels(skip_web_ui=False, enable_canvas=True)
    assert captured["OMNIGENT_ENABLE_CANVAS"] == "1"

    captured.clear()
    deploy_mod._build_wheels(skip_web_ui=True, enable_canvas=True)
    assert captured["SKIP_WEB_UI"] == "1"
    assert "EXTERNALIZE_WEB_UI" not in captured
    assert "OMNIGENT_ENABLE_CANVAS" not in captured


def test_build_wheels_mode_ignores_the_ambient_environment(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The flag decides the build mode, not whatever the caller exported.

    An ambient ``SKIP_WEB_UI=1`` left over from an earlier API-only build would
    otherwise be inherited through ``os.environ.copy()`` and make ``build.sh``
    skip the SPA for a deploy that explicitly asked to include it. The mirror
    case matters too: a stale ``EXTERNALIZE_WEB_UI`` must not make an API-only
    build try to package a bundle that was never built.
    """
    captured: dict[str, str] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> None:
        captured.update(kwargs["env"])  # type: ignore[arg-type]
        (tmp_path / "dist").mkdir(exist_ok=True)
        (tmp_path / "dist" / "omnigent-1.0-py3-none-any.whl").write_bytes(b"w")

    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)

    monkeypatch.setenv("SKIP_WEB_UI", "1")
    monkeypatch.setenv("EXTERNALIZE_WEB_UI", "stale")
    monkeypatch.setenv("OMNIGENT_ENABLE_CANVAS", "true")

    deploy_mod._build_wheels(skip_web_ui=False)
    assert "SKIP_WEB_UI" not in captured
    assert captured["EXTERNALIZE_WEB_UI"] == "1"
    assert "OMNIGENT_ENABLE_CANVAS" not in captured

    captured.clear()
    deploy_mod._build_wheels(skip_web_ui=True)
    assert captured["SKIP_WEB_UI"] == "1"
    assert "EXTERNALIZE_WEB_UI" not in captured


def test_stage_web_ui_copies_archive(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    archive = _make_spa_archive(dist)
    src = tmp_path / "deploy" / "databricks" / "src"
    src.mkdir(parents=True)
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod, "_src_dir", lambda: src)

    staged = deploy_mod._stage_web_ui(skip_web_ui=False)

    assert staged == src / "web-ui.tar.gz"
    assert staged is not None and staged.read_bytes() == archive.read_bytes()


def test_stage_web_ui_replaces_a_stale_loose_copy(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A prior loose deployment must not leave orphaned chunks behind."""
    dist = tmp_path / "dist"
    dist.mkdir()
    archive = _make_spa_archive(dist)
    src = tmp_path / "src"
    (src / "web-ui" / "assets").mkdir(parents=True)
    (src / "web-ui" / "assets" / "index-STALE.js").write_text("old")
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod, "_src_dir", lambda: src)

    deploy_mod._stage_web_ui(skip_web_ui=False)

    assert not (src / "web-ui").exists()
    assert (src / "web-ui.tar.gz").read_bytes() == archive.read_bytes()


def test_stage_web_ui_skip_web_ui_clears_assets(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An API-only deploy must not leave a previous SPA behind."""
    src = tmp_path / "src"
    src.mkdir()
    _make_spa_archive(tmp_path / "dist")
    (src / "web-ui").mkdir()
    (src / "web-ui.tar.gz").write_bytes(b"stale")
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod, "_src_dir", lambda: src)

    # Even if --skip-build leaves an old archive in dist/, API-only staging
    # must clear both forms from the app source and not copy it back.
    assert deploy_mod._dist_web_ui_archive() == tmp_path / "dist" / "web-ui.tar.gz"
    deploy_mod._stage_web_ui(skip_web_ui=True)

    assert not (src / "web-ui").exists()
    assert not (src / "web-ui.tar.gz").exists()


def test_stage_web_ui_requires_a_build(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "dist").mkdir()
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod, "_src_dir", lambda: src)

    assert deploy_mod._stage_web_ui(skip_web_ui=False) is None


def test_oversize_archive_fails_before_upload(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The uploaded archive itself must stay under the Workspace file cap."""
    dist = tmp_path / "dist"
    dist.mkdir()
    archive = _make_spa_archive(dist, asset_bytes=deploy_mod._WORKSPACE_FILE_LIMIT_BYTES + 1)
    # Repeated bytes compress too well; append incompressible bytes to make the
    # uploaded archive exceed the cap, which is the relevant Workspace check.
    with archive.open("ab") as handle:
        handle.write(os.urandom(deploy_mod._WORKSPACE_FILE_LIMIT_BYTES))
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod, "_src_dir", lambda: src)

    deploy_mod._stage_web_ui(skip_web_ui=False)
    with pytest.raises(SystemExit, match="exceed the Databricks Apps 10 MB"):
        deploy_mod._assert_app_files_under_limit()
    assert not (src / "web-ui").exists()
