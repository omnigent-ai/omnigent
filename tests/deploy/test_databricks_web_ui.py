"""The Databricks Apps deploy ships the SPA outside the wheel.

Databricks Apps uploads the app source directory as Workspace files, and the
Workspace import API rejects any single file over 10 MB. The built SPA is ~25 MB
of assets, which takes the main wheel over that cap and fails the deploy before
anything is uploaded. So ``build.sh`` moves the SPA out of the wheel's package
data and the deploy ships it as loose files in ``src/web-ui/``, which
``src/app.py`` points the server at via ``OMNIGENT_WEB_UI_DIST``.

These tests pin each link in that chain, including the env var itself — the
server reads it at import time, so it is checked in a subprocess.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
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


def test_app_py_points_server_at_loose_assets_before_importing_it() -> None:
    """The path is read at import time, so the setup must come first."""
    source = _APP_PY.read_text()
    setup_at = source.index('os.environ.setdefault("OMNIGENT_WEB_UI_DIST"')
    import_at = source.index("from omnigent.server.app import create_app")
    assert setup_at < import_at, "OMNIGENT_WEB_UI_DIST is set too late to take effect"
    assert 'Path(__file__).parent / "web-ui"' in source


def test_build_sh_moves_spa_out_of_the_wheel() -> None:
    source = _BUILD_SH.read_text()
    assert 'mv omnigent/server/static/web-ui "${web_ui_out_dir}"' in source
    # The move has to happen while the SPA build is in scope, before uv build.
    assert source.index("WEB_UI_OUT_NAME") < source.index("uv build --wheel --out-dir dist/ .")


def test_build_sh_opts_the_backend_out_of_rebuilding_the_spa() -> None:
    """setup.py rebuilds the SPA into the package when the bundle is missing.

    Without the opt-out it puts the assets straight back into the wheel, undoing
    the move, so the export has to land before any wheel build.
    """
    source = _BUILD_SH.read_text()
    assert "export OMNIGENT_SKIP_WEB_UI=true" in source
    assert source.index("export OMNIGENT_SKIP_WEB_UI=true") < source.index(
        "uv build --wheel --out-dir dist/ sdks/python-client/"
    )


def test_build_sh_moves_the_spa_and_opts_out_when_run_for_real(tmp_path: Path) -> None:
    """Execute the real script with fake ``pnpm``/``uv`` and check both effects.

    Text assertions cannot show that the move and the opt-out actually happen
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

    # The same location deploy.py passes: <repo>/dist/web-ui.
    out_dir = repo / "dist" / "web-ui"
    env = os.environ.copy()
    env.update(
        {
            "COMMAND_LOG": str(command_log),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "WEB_UI_OUT_NAME": out_dir.name,
        }
    )
    env.pop("OMNIGENT_SKIP_WEB_UI", None)
    env.pop("SKIP_WEB_UI", None)

    subprocess.run(["bash", str(script)], cwd=repo, env=env, check=True)

    # The SPA moved out of the package tree and into dist/<WEB_UI_OUT_NAME>.
    assert (out_dir / "index.html").is_file()
    assert (out_dir / "assets" / "index-abc.js").is_file()
    assert not (repo / "omnigent" / "server" / "static" / "web-ui").exists()

    # And every wheel build saw the opt-out, so setup.py cannot rebuild it back in.
    uv_calls = [line for line in command_log.read_text().splitlines() if line.startswith("uv|")]
    assert len(uv_calls) == 3
    assert all(call.split("|", 2)[1] == "true" for call in uv_calls), uv_calls


@pytest.mark.parametrize(
    "out_name",
    [
        pytest.param("../../precious", id="parent-traversal"),
        pytest.param("web-ui/../../../precious", id="traversal-after-a-valid-prefix"),
        pytest.param("..", id="dot-dot"),
        pytest.param(".", id="dot"),
        pytest.param("/", id="the-filesystem-root"),
        pytest.param("/tmp", id="an-absolute-path"),
        pytest.param("nested/name", id="a-nested-path"),
        pytest.param("name with spaces", id="whitespace"),
        pytest.param("name;rm -rf /", id="shell-metacharacters"),
        pytest.param("$HOME", id="an-expansion-attempt"),
    ],
)
def test_build_sh_refuses_a_web_ui_out_name_that_is_not_a_bare_name(
    tmp_path: Path, out_name: str
) -> None:
    """The move destination is ``rm -rf``'d, so only a bare name is accepted.

    A caller-supplied *path* can escape the repo through ``..`` or a symlink no
    matter how it is string-matched — a lexical prefix check on
    ``<repo>/dist/...`` passes ``<repo>/dist/../../home``. Accepting only a name
    that cannot contain ``/`` or ``.`` removes the entire class: the script
    anchors it under its own freshly created ``dist/``.

    The script must refuse, exit non-zero, and leave everything untouched.
    """
    repo = tmp_path / "repo"
    script = repo / "deploy" / "databricks" / "build.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(_BUILD_SH, script)

    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "important.txt").write_text("IRREPLACEABLE")
    (repo / "omnigent").mkdir()
    (repo / "omnigent" / "__init__.py").write_text("# source")

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
    _write_executable(fake_bin / "uv", "#!/usr/bin/env bash\nmkdir -p dist\ntouch dist/f.whl\n")

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["WEB_UI_OUT_NAME"] = out_name
    env.pop("OMNIGENT_SKIP_WEB_UI", None)
    env.pop("SKIP_WEB_UI", None)
    env.pop("WEB_UI_OUT_DIR", None)

    result = subprocess.run(
        ["bash", str(script)], cwd=repo, env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode != 0, result.stdout
    assert "WEB_UI_OUT_NAME must be a bare directory name" in result.stderr
    # Nothing outside dist/ was touched.
    assert (precious / "important.txt").read_text() == "IRREPLACEABLE"
    assert (repo / "omnigent" / "__init__.py").is_file()


def test_build_sh_leaves_the_spa_in_the_wheel_without_web_ui_out_dir(tmp_path: Path) -> None:
    """The move is opt-in: an ordinary build is byte-for-byte unaffected.

    Without ``WEB_UI_OUT_NAME`` the SPA stays in the package tree and the wheel
    hook is *not* opted out, so a plain ``build.sh`` behaves exactly as on main.
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
    env.pop("WEB_UI_OUT_NAME", None)

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


def test_build_wheels_requests_the_spa_outside_the_wheel(
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
    assert captured["WEB_UI_OUT_NAME"] == "web-ui"
    assert "SKIP_WEB_UI" not in captured

    captured.clear()
    deploy_mod._build_wheels(skip_web_ui=True)
    assert captured["SKIP_WEB_UI"] == "1"
    assert "WEB_UI_OUT_NAME" not in captured


def test_build_wheels_mode_ignores_the_ambient_environment(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The flag decides the build mode, not whatever the caller exported.

    An ambient ``SKIP_WEB_UI=1`` left over from an earlier API-only build would
    otherwise be inherited through ``os.environ.copy()`` and make ``build.sh``
    skip the SPA for a deploy that explicitly asked to include it. The mirror
    case matters too: a stale ``WEB_UI_OUT_NAME`` must not make an API-only build
    try to move a bundle that was never built.
    """
    captured: dict[str, str] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> None:
        captured.update(kwargs["env"])  # type: ignore[arg-type]
        (tmp_path / "dist").mkdir(exist_ok=True)
        (tmp_path / "dist" / "omnigent-1.0-py3-none-any.whl").write_bytes(b"w")

    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod.subprocess, "run", fake_run)

    monkeypatch.setenv("SKIP_WEB_UI", "1")
    monkeypatch.setenv("WEB_UI_OUT_NAME", "stale")

    deploy_mod._build_wheels(skip_web_ui=False)
    assert "SKIP_WEB_UI" not in captured
    assert captured["WEB_UI_OUT_NAME"] == "web-ui"

    captured.clear()
    deploy_mod._build_wheels(skip_web_ui=True)
    assert captured["SKIP_WEB_UI"] == "1"
    assert "WEB_UI_OUT_NAME" not in captured


def test_sync_src_web_ui_copies_build_into_app_source(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    _make_spa(dist)
    src = tmp_path / "deploy" / "databricks" / "src"
    src.mkdir(parents=True)
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod, "_src_dir", lambda: src)

    deploy_mod._sync_src_web_ui(skip_web_ui=False)

    assert (src / "web-ui" / "index.html").is_file()
    assert (src / "web-ui" / "assets" / "index-abc123.js").is_file()


def test_sync_src_web_ui_replaces_a_stale_copy(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Vite emits hashed chunk names; stale assets must not accumulate."""
    dist = tmp_path / "dist"
    dist.mkdir()
    _make_spa(dist)
    src = tmp_path / "src"
    (src / "web-ui" / "assets").mkdir(parents=True)
    (src / "web-ui" / "assets" / "index-STALE.js").write_text("old")
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod, "_src_dir", lambda: src)

    deploy_mod._sync_src_web_ui(skip_web_ui=False)

    assert not (src / "web-ui" / "assets" / "index-STALE.js").exists()
    assert (src / "web-ui" / "assets" / "index-abc123.js").is_file()


def test_sync_src_web_ui_skip_web_ui_clears_assets(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An API-only deploy must not leave a previous SPA behind."""
    src = tmp_path / "src"
    _make_spa(src)
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod, "_src_dir", lambda: src)

    deploy_mod._sync_src_web_ui(skip_web_ui=True)

    assert not (src / "web-ui").exists()


def test_sync_src_web_ui_requires_a_build(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod, "_src_dir", lambda: src)

    with pytest.raises(SystemExit, match="no SPA build at"):
        deploy_mod._sync_src_web_ui(skip_web_ui=False)


def test_oversize_asset_fails_before_upload(
    deploy_mod: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A single Vite chunk over 10 MB would be rejected by the Workspace."""
    dist = tmp_path / "dist"
    dist.mkdir()
    _make_spa(dist, asset_bytes=deploy_mod._WORKSPACE_FILE_LIMIT_BYTES + 1)
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(deploy_mod, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(deploy_mod, "_src_dir", lambda: src)

    with pytest.raises(SystemExit, match="over the 10 MB Workspace file cap"):
        deploy_mod._sync_src_web_ui(skip_web_ui=False)
    assert not (src / "web-ui").exists()
