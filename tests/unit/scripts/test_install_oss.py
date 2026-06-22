"""Tests for the OSS installer shell script (``scripts/install_oss.sh``).

The installer is POSIX ``sh``. Its argument parsing and URL/path/profile
derivation are pure string logic that must stay correct across the shapes
users actually pass (``--repo git@host:org/repo.git``, ``--version X``,
bare ``https://`` URLs) and across macOS/Linux + zsh/bash combinations.

Strategy: the script ends in a single ``main "$@"`` call. We strip that one
line to get a sourceable library, then drive individual functions from a
fresh ``sh -c`` per case. Platform branches that shell out to ``uname`` are
made deterministic by defining a ``uname`` shell function in the snippet
(a function shadows the external command), and ``linux_pkg_install_cmd``'s
package-manager probe is driven by putting fake executables on ``PATH``.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

# repo-root/scripts/install_oss.sh, from tests/scripts/test_install_oss.py
INSTALLER = Path(__file__).resolve().parents[3] / "scripts" / "install_oss.sh"

# Resolve sh up front: some tests override PATH to probe package-manager
# detection, which would otherwise hide the launcher from subprocess.
SH = shutil.which("sh") or "/bin/sh"

# Agent credentials must not leak into the script under test (CLAUDE.md).
_STRIP_ENV = ("DATABRICKS_TOKEN", "ANTHROPIC_API_KEY", "CODEX", "CLAUDE_CODE")


@pytest.fixture(scope="session")
def lib(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return a sourceable copy of the installer with its ``main "$@"`` call removed.

    Sourcing the original would run the full installer (network, uv, PATH
    edits). Dropping the single trailing invocation leaves every function
    definition intact and side-effect-free to source.
    """
    text = INSTALLER.read_text()
    lines = text.splitlines(keepends=True)
    stripped = [ln for ln in lines if ln.rstrip("\n") != 'main "$@"']
    assert len(stripped) == len(lines) - 1, (
        'Expected exactly one `main "$@"` invocation to strip; the script '
        "shape changed and this harness needs updating."
    )
    out = tmp_path_factory.mktemp("install_oss") / "lib.sh"
    out.write_text("".join(stripped))
    return out


def run(
    lib: Path, snippet: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Source ``lib`` and run ``snippet`` in a fresh POSIX shell.

    :param snippet: shell run after the library is sourced; its exit status
        and stdout are the assertion surface.
    :param env: overrides merged onto a credential-scrubbed copy of os.environ.
    """
    base = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    if env:
        base.update(env)
    program = f". {shlex.quote(str(lib))}\n{snippet}\n"
    return subprocess.run(
        [SH, "-c", program], capture_output=True, text=True, env=base, timeout=30
    )


def test_parse_args_sets_flags(lib: Path) -> None:
    """``--non-interactive`` and ``--verbose`` flip their globals to ``true``."""
    r = run(lib, 'parse_args --non-interactive --verbose; echo "$NON_INTERACTIVE $VERBOSE"')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "true true", (
        f"Both flags should set their globals to 'true', got {r.stdout.strip()!r}."
    )


def test_parse_args_captures_version_and_repo(lib: Path) -> None:
    """``--version`` and ``--repo`` capture the value that follows each flag."""
    r = run(lib, 'parse_args --version 1.2.3 --repo https://x/y; echo "$VERSION|$REPO_URL"')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "1.2.3|https://x/y", (
        f"Version and repo should be captured verbatim, got {r.stdout.strip()!r}."
    )


@pytest.mark.parametrize("args", ["--version", "--repo", "--frobnicate"])
def test_parse_args_rejects_bad_input(lib: Path, args: str) -> None:
    """A value-less ``--version``/``--repo`` and any unknown flag exit non-zero.

    These are the guards that stop a typo'd invocation from silently
    installing the wrong thing.
    """
    r = run(lib, f"parse_args {args}")
    assert r.returncode != 0, (
        f"`parse_args {args}` should fail, but exited 0 with stdout {r.stdout!r}."
    )


def test_normalize_repo_url_empty_means_pypi(lib: Path) -> None:
    """No ``--repo`` leaves ``INSTALL_URL`` empty — the default PyPI wheel path."""
    r = run(lib, 'REPO_URL=; normalize_repo_url; echo "[$INSTALL_URL]"')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]", (
        f"Empty REPO_URL must yield an empty INSTALL_URL, got {r.stdout.strip()!r}."
    )


@pytest.mark.parametrize(
    ("repo_url", "expected"),
    [
        # bare https/http -> prefixed with git+ so uv treats it as a VCS source
        ("https://github.com/o/r", "git+https://github.com/o/r"),
        ("http://example.com/o/r", "git+http://example.com/o/r"),
        # already a pip VCS URL -> passed through untouched
        ("git+https://github.com/o/r", "git+https://github.com/o/r"),
        ("git+ssh://git@host/o/r", "git+ssh://git@host/o/r"),
        # bare ssh:// -> git+ssh://
        ("ssh://git@host/o/r", "git+ssh://git@host/o/r"),
        # scp-like git@host:org/repo.git -> git+ssh://git@host/org/repo.git
        ("git@host:org/repo.git", "git+ssh://git@host/org/repo.git"),
    ],
)
def test_normalize_repo_url_shapes(lib: Path, repo_url: str, expected: str) -> None:
    """Each supported ``--repo`` URL shape normalizes to the uv VCS spelling."""
    r = run(
        lib, f'REPO_URL={shlex.quote(repo_url)}; normalize_repo_url; printf "%s" "$INSTALL_URL"'
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected, (
        f"{repo_url!r} should normalize to {expected!r}, got {r.stdout!r}."
    )


def test_normalize_repo_url_unsupported_fails(lib: Path) -> None:
    """An unrecognized ``--repo`` value fails loudly instead of guessing."""
    r = run(lib, "REPO_URL=not-a-url; normalize_repo_url")
    assert r.returncode != 0, "An unsupported --repo URL must fail, not be silently accepted."
    assert "Unsupported --repo URL" in r.stderr, (
        f"The error should name the offending input, got stderr {r.stderr!r}."
    )


def test_normalize_repo_url_version_repo_conflict_fails(lib: Path) -> None:
    """``--version`` (a PyPI pin) combined with ``--repo`` (a source build) is rejected."""
    r = run(lib, "REPO_URL=https://x/y; VERSION=1.2.3; normalize_repo_url")
    assert r.returncode != 0, "--version + --repo is contradictory and must fail."
    assert "--version" in r.stderr and "--repo" in r.stderr, (
        f"The error should explain the version/repo conflict, got {r.stderr!r}."
    )


@pytest.mark.parametrize(
    ("install_url", "expected_rc"),
    [("git+https://x/y", 0), ("", 1)],
)
def test_building_from_source(lib: Path, install_url: str, expected_rc: int) -> None:
    """``building_from_source`` is true only when ``INSTALL_URL`` is non-empty."""
    r = run(lib, f"INSTALL_URL={shlex.quote(install_url)}; building_from_source")
    assert r.returncode == expected_rc, (
        f"INSTALL_URL={install_url!r} should give rc {expected_rc}, got {r.returncode}."
    )


def test_path_contains(lib: Path) -> None:
    """``path_contains`` matches a whole PATH segment, not a substring."""
    hit = run(lib, "PATH=/a:/b/bin:/c; path_contains /b/bin")
    miss = run(lib, "PATH=/a:/b/bin:/c; path_contains /b")
    assert hit.returncode == 0, "A directory present as a full PATH segment should match."
    assert miss.returncode == 1, "/b must not match the /b/bin segment (no substring matches)."


@pytest.mark.parametrize(
    ("uname", "shell", "expected"),
    [
        ("Darwin", "/usr/bin/zsh", ".zprofile"),
        ("Darwin", "/bin/bash", ".bash_profile"),
        ("Linux", "/usr/bin/zsh", ".zshrc"),
        ("Linux", "/bin/bash", ".bashrc"),
        ("Linux", "/usr/bin/fish", ".profile"),  # unknown shell -> POSIX fallback
    ],
)
def test_pick_profile(lib: Path, uname: str, shell: str, expected: str) -> None:
    """The shell profile is chosen from the OS + login shell pair."""
    home = "/tmp/fakehome"
    r = run(
        lib,
        f"uname() {{ echo {uname}; }}; pick_profile",
        env={"HOME": home, "SHELL": shell},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"{home}/{expected}", (
        f"{uname}+{shell} should pick {expected}, got {r.stdout.strip()!r}."
    )


@pytest.mark.parametrize(
    ("uname", "ok"), [("Darwin", True), ("Linux", True), ("MINGW64_NT", False)]
)
def test_check_platform(lib: Path, uname: str, ok: bool) -> None:
    """Only macOS and Linux are supported; anything else fails loudly."""
    r = run(lib, f"uname() {{ echo {uname}; }}; check_platform")
    if ok:
        assert r.returncode == 0, f"{uname} should be accepted, got stderr {r.stderr!r}."
    else:
        assert r.returncode != 0, f"{uname} should be rejected as unsupported."
        assert "macOS and Linux only" in r.stderr


def test_spinner_frame_cycles(lib: Path) -> None:
    """The spinner cycles ``-`` ``\\`` ``|`` ``/`` and wraps modulo 4."""
    r = run(
        lib, "spinner_frame 0; spinner_frame 1; spinner_frame 2; spinner_frame 3; spinner_frame 4"
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == "-\\|/-", f"Spinner should cycle and wrap at 4, got {r.stdout!r}."


def test_prompt_yes_no_non_interactive_declines(lib: Path) -> None:
    """In ``--non-interactive`` mode the prompt declines without reading input.

    This is what keeps unattended installs from blocking on a TTY read.
    """
    r = run(lib, "NON_INTERACTIVE=true; prompt_yes_no 'install foo?'")
    assert r.returncode == 1, "Non-interactive prompts must default to 'no' (rc 1)."
    assert r.stdout == "", f"Nothing should be printed to a non-TTY prompt, got {r.stdout!r}."


def _bindir(tmp_path: Path, *tools: str) -> str:
    """Create a directory holding empty executable stubs for ``tools`` and return it.

    ``linux_pkg_install_cmd`` only probes presence via ``command -v``, so the
    stubs need to be executable but need not do anything.
    """
    for tool in tools:
        stub = tmp_path / tool
        stub.write_text("#!/bin/sh\n")
        stub.chmod(0o755)
    return str(tmp_path)


@pytest.mark.parametrize(
    ("tools", "expected"),
    [
        (("dnf",), "sudo dnf install -y tmux"),
        (("yum",), "sudo yum install -y tmux"),
        (("pacman",), "sudo pacman -S --noconfirm tmux"),
        (("zypper",), "sudo zypper install -y tmux"),
        # apt-get takes precedence when several managers are present
        (("apt-get", "dnf", "yum"), "sudo apt-get install -y tmux"),
    ],
)
def test_linux_pkg_install_cmd(
    lib: Path, tmp_path: Path, tools: tuple[str, ...], expected: str
) -> None:
    """The package-manager command matches the detected manager, apt-get first."""
    bindir = _bindir(tmp_path, *tools)
    r = run(lib, "linux_pkg_install_cmd tmux", env={"PATH": bindir})
    assert r.returncode == 0, r.stderr
    assert r.stdout == expected, f"Tools {tools} should yield {expected!r}, got {r.stdout!r}."


def test_linux_pkg_install_cmd_none_present(lib: Path, tmp_path: Path) -> None:
    """With no known package manager on PATH the helper emits nothing."""
    bindir = _bindir(tmp_path)  # empty dir
    r = run(lib, "linux_pkg_install_cmd tmux", env={"PATH": bindir})
    assert r.returncode == 0, r.stderr
    assert r.stdout == "", f"No package manager should produce empty output, got {r.stdout!r}."


def test_check_bubblewrap_noop_on_macos(lib: Path, tmp_path: Path) -> None:
    """On macOS the bubblewrap check is a silent no-op — seatbelt needs no binary."""
    bindir = _bindir(tmp_path)
    r = run(lib, "uname() { echo Darwin; }; check_bubblewrap", env={"PATH": bindir})
    assert r.returncode == 0, r.stderr
    assert "bubblewrap" not in (r.stdout + r.stderr), (
        f"macOS should say nothing about bubblewrap, got stdout={r.stdout!r} stderr={r.stderr!r}."
    )


def test_check_bubblewrap_present_on_linux(lib: Path, tmp_path: Path) -> None:
    """When ``bwrap`` is on PATH the Linux check reports it available."""
    bindir = _bindir(tmp_path, "bwrap")
    r = run(lib, "uname() { echo Linux; }; check_bubblewrap", env={"PATH": bindir})
    assert r.returncode == 0, r.stderr
    assert "bubblewrap (bwrap) is available" in r.stdout, (
        f"A present bwrap should be reported available, got {r.stdout!r}."
    )


def test_check_bubblewrap_missing_warns_with_pkg_cmd(lib: Path, tmp_path: Path) -> None:
    """Missing ``bwrap`` warns (non-fatally) with the detected install command.

    The native harness sandbox needs bwrap on Linux, but the installer must not
    abort the whole install over it — it warns and continues (exit 0).
    """
    bindir = _bindir(tmp_path, "apt-get")  # package manager present, but no bwrap
    r = run(
        lib,
        "uname() { echo Linux; }; NON_INTERACTIVE=true; check_bubblewrap",
        env={"PATH": bindir},
    )
    assert r.returncode == 0, "A missing bwrap must warn, not fail (exit 0)."
    assert "sudo apt-get install -y bubblewrap" in r.stderr, (
        f"The warning should name the detected install command, got {r.stderr!r}."
    )


def test_check_bubblewrap_missing_no_pkg_manager_warns_generically(
    lib: Path, tmp_path: Path
) -> None:
    """Missing ``bwrap`` with no known package manager falls back to a generic warning."""
    bindir = _bindir(tmp_path)  # no bwrap, no package manager
    r = run(
        lib,
        "uname() { echo Linux; }; NON_INTERACTIVE=true; check_bubblewrap",
        env={"PATH": bindir},
    )
    assert r.returncode == 0, "A missing bwrap must warn, not fail (exit 0)."
    assert "Install it with your package manager" in r.stderr, (
        f"With no package manager the warning should be generic, got {r.stderr!r}."
    )
