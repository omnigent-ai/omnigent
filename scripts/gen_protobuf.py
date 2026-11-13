"""Generate the protobuf bindings for every API schema under ``omnigent/api/``.

Each API ships a protobuf schema (e.g. ``omnigent/api/routing/v1/routing.proto``)
and the runtime imports its generated modules: ``*_pb2`` for messages, plus
``*_connect`` (Connect RPC server and client stubs) for schemas that declare a
service. Generated code is checked in (so a plain ``pip install`` / editor / type
checker sees it without a build step), and this script is the one blessed way to
regenerate it — run it whenever a ``.proto`` changes and commit the result.

It shells out to ``grpc_tools.protoc`` (the ``grpcio-tools`` dev dependency),
which bundles both the compiler and the well-known-type protos, so imports such
as ``google/protobuf/struct.proto`` resolve with no system ``protoc`` install.
Service stubs come from the ``protoc-gen-connectrpc`` plugin (also a dev
dependency), which emits nothing for schemas without a service.

Pass ``--check`` to verify the committed bindings are up to date without
writing: it regenerates into a temp dir, compares, and exits non-zero (printing
the drift) if a ``.proto`` was edited without regenerating. The
``protobuf-bindings-fresh`` pre-commit hook runs it in this mode.

Usage::

    python scripts/gen_protobuf.py            # (re)generate in place
    python scripts/gen_protobuf.py --check     # verify freshness (CI / hook)
"""

from __future__ import annotations

import argparse
import filecmp
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Repo root (this file lives in ``scripts/``). ``--proto_path`` is rooted here so
# generated package paths match the imports the runtime uses (and each proto's
# ``package`` line), e.g. ``omnigent/api/routing/v1/routing_pb2``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_API_ROOT = Path("omnigent/api")
# The plugin sits next to the interpreter in the venv that has the dev dependencies.
_CONNECT_PLUGIN = Path(sys.executable).parent / "protoc-gen-connectrpc"


def _protos() -> list[Path]:
    """Every ``.proto`` under the API root, relative to the repo root."""
    return sorted(p.relative_to(_REPO_ROOT) for p in (_REPO_ROOT / _API_ROOT).rglob("*.proto"))


def _run_protoc(out_dir: Path) -> None:
    """Compile every API schema into ``out_dir`` (rooted like the repo)."""
    if importlib.util.find_spec("grpc_tools") is None or not _CONNECT_PLUGIN.exists():
        raise SystemExit(
            "grpc_tools or protoc-gen-connectrpc not found. Install the dev dependencies first:\n"
            "  uv sync --group lint"
        )
    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"--proto_path={_REPO_ROOT}",
        f"--python_out={out_dir}",
        f"--pyi_out={out_dir}",
        f"--plugin=protoc-gen-connectrpc={_CONNECT_PLUGIN}",
        f"--connectrpc_out={out_dir}",
        # Stubs use the google protobuf messages that ``--python_out`` emits.
        "--connectrpc_opt=protobuf=google",
        *(str(p) for p in _protos()),
    ]
    subprocess.run(cmd, check=True, cwd=_REPO_ROOT)


def _generated(root: Path) -> list[Path]:
    """Generated files under ``root``, relative to it."""
    return sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())


def _committed() -> set[Path]:
    """Generated-looking files in the repo's API tree, relative to the repo root."""
    api = _REPO_ROOT / _API_ROOT
    patterns = ("*_pb2.py", "*_pb2.pyi", "*_connect.py")
    return {p.relative_to(_REPO_ROOT) for pattern in patterns for p in api.rglob(pattern)}


def _generate() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _run_protoc(tmp_dir)
        outputs = _generated(tmp_dir)
        for p in outputs:
            shutil.copyfile(tmp_dir / p, _REPO_ROOT / p)
        # Bindings whose schema or service is gone would otherwise still import.
        for p in _committed() - set(outputs):
            (_REPO_ROOT / p).unlink()
    print(f"Generated {', '.join(str(p) for p in outputs)}")
    return 0


def _check() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _run_protoc(tmp_dir)
        generated = _generated(tmp_dir)
        stale = [
            p
            for p in generated
            if not (_REPO_ROOT / p).exists()
            or not filecmp.cmp(tmp_dir / p, _REPO_ROOT / p, shallow=False)
        ]
    # A binding whose schema or service was removed would still import.
    orphaned = sorted(_committed() - set(generated))
    if orphaned:
        print(
            f"Protobuf bindings without a schema: {', '.join(str(p) for p in orphaned)}\n"
            "Delete them, or restore their .proto.",
            file=sys.stderr,
        )
        return 1
    if stale:
        names = ", ".join(str(p) for p in stale)
        print(
            f"Protobuf bindings are out of date: {names}\n"
            "Regenerate and commit them:\n"
            "  python scripts/gen_protobuf.py",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed bindings match the schemas without writing",
    )
    args = parser.parse_args()
    return _check() if args.check else _generate()


if __name__ == "__main__":
    raise SystemExit(main())
