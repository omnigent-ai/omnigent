"""Build standalone Python packages with the repository's legal files."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LEGAL_FILES = ("LICENSE", "NOTICE")
LICENSE_FILES_DECLARATION = 'license-files = ["LICENSE", "NOTICE"]'
PACKAGE_DIRS = {
    "omnigent-client": Path("sdks/python-client"),
    "omnigent-ui-sdk": Path("sdks/ui"),
    "omnigent-slack": Path("integrations/slack"),
}


@contextmanager
def staged_package(package_dir: Path) -> Iterator[Path]:
    """Stage a package beside its source and add the canonical legal files."""
    source = (REPO_ROOT / package_dir).resolve()
    with tempfile.TemporaryDirectory(prefix=f".{source.name}-build-", dir=source.parent) as tmp:
        staged = Path(tmp)
        shutil.copytree(
            source,
            staged,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache", "dist"),
        )
        for filename in LEGAL_FILES:
            shutil.copy2(REPO_ROOT / filename, staged / filename)
        _declare_license_files(staged / "pyproject.toml")
        yield staged


def _declare_license_files(pyproject: Path) -> None:
    """Declare the legal files that exist only in the staged project."""
    source = pyproject.read_text(encoding="utf-8")
    anchor = 'license = "Apache-2.0"\n'
    if source.count(anchor) != 1:
        raise ValueError(f"expected one Apache-2.0 license declaration in {pyproject}")
    pyproject.write_text(
        source.replace(anchor, f"{anchor}{LICENSE_FILES_DECLARATION}\n"), encoding="utf-8"
    )


def build_packages(package_names: Sequence[str], out_dir: Path, *, wheel_only: bool) -> None:
    """Build selected packages from temporary, self-contained source trees."""
    output = out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for package_name in package_names:
        with staged_package(PACKAGE_DIRS[package_name]) as staged:
            command = [sys.executable, "-m", "build", "--outdir", str(output)]
            if wheel_only:
                command.append("--wheel")
            command.append(str(staged))
            subprocess.run(command, cwd=REPO_ROOT, check=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", nargs="+", choices=PACKAGE_DIRS)
    parser.add_argument("--out-dir", type=Path, default=Path("dist"))
    parser.add_argument("--wheel", action="store_true", help="Build wheels without sdists")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Build the requested standalone packages."""
    args = parse_args(argv)
    build_packages(args.package, args.out_dir, wheel_only=args.wheel)


if __name__ == "__main__":
    main()
