"""Generate the routing API protobuf bindings from its ``routing.proto``.

The routing API ships a protobuf schema
(``omnigent/api/routing/v1/routing.proto``); the runtime imports the generated
``omnigent.api.routing.v1.routing_pb2`` module. Generated
code is checked in (so a plain ``pip install`` / editor / mypy sees it without a
build step), and this script is the one blessed way to regenerate it — run it
whenever ``routing.proto`` changes and commit the result.

It shells out to ``grpc_tools.protoc`` (the ``grpcio-tools`` dev dependency),
which bundles both the compiler and the well-known-type protos, so the
``import "google/protobuf/struct.proto"`` in the schema resolves with no system
``protoc`` install.

Pass ``--check`` to verify the committed bindings are up to date without
writing: it regenerates into a temp dir, compares, and exits non-zero (printing
the drift) if ``routing.proto`` was edited without regenerating. The
``routing-pb2-fresh`` pre-commit hook runs it in this mode.

Usage::

    python scripts/gen_routing_pb2.py            # (re)generate in place
    python scripts/gen_routing_pb2.py --check     # verify freshness (CI / hook)
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Repo root (this file lives in ``scripts/``). ``--proto_path`` is rooted here so
# the generated package path is ``omnigent/api/routing/v1/routing_pb2`` —
# matching the import the runtime uses (and the proto's ``package`` line).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROTO = Path("omnigent/api/routing/v1/routing.proto")
# The two files protoc emits for the schema, relative to the repo root.
_OUTPUTS = (
    Path("omnigent/api/routing/v1/routing_pb2.py"),
    Path("omnigent/api/routing/v1/routing_pb2.pyi"),
)


def _run_protoc(out_dir: Path) -> None:
    """Compile ``routing.proto`` into ``out_dir`` (rooted like the repo)."""
    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"--proto_path={_REPO_ROOT}",
        f"--python_out={out_dir}",
        f"--pyi_out={out_dir}",
        str(_PROTO),
    ]
    try:
        subprocess.run(cmd, check=True, cwd=_REPO_ROOT)
    except FileNotFoundError:
        raise SystemExit(
            "grpc_tools.protoc not found. Install the dev dependencies first:\n"
            "  uv sync --extra dev"
        ) from None


def _generate() -> int:
    _run_protoc(_REPO_ROOT)
    print(f"Generated {', '.join(str(p) for p in _OUTPUTS)}")
    return 0


def _check() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _run_protoc(tmp_dir)
        stale = [
            p
            for p in _OUTPUTS
            if not (tmp_dir / p).exists()
            or not (_REPO_ROOT / p).exists()
            or not filecmp.cmp(tmp_dir / p, _REPO_ROOT / p, shallow=False)
        ]
    if stale:
        names = ", ".join(str(p) for p in stale)
        print(
            f"Routing protobuf bindings are out of date: {names}\n"
            "Regenerate and commit them:\n"
            "  python scripts/gen_routing_pb2.py",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed bindings match the schema without writing",
    )
    args = parser.parse_args()
    if shutil.which(sys.executable) is None:  # pragma: no cover - defensive
        raise SystemExit(f"python interpreter not found: {sys.executable}")
    return _check() if args.check else _generate()


if __name__ == "__main__":
    raise SystemExit(main())
