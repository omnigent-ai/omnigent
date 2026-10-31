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

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pexpect
import pytest

# repo-root/scripts/install_oss.sh, from tests/scripts/test_install_oss.py
INSTALLER = Path(__file__).resolve().parents[2] / "scripts" / "install_oss.sh"

# Resolve sh up front: some tests override PATH to probe package-manager
# detection, which would otherwise hide the launcher from subprocess.
SH = shutil.which("sh") or "/bin/sh"


@pytest.mark.posix_only
@pytest.mark.parametrize(
    ("platform", "version", "answer", "upgrade_version", "upgrade_status"),
    [
        pytest.param("Linux", "3.3a", None, "3.3a", 0, id="linux-supported"),
        pytest.param("Linux", "3.0", None, "3.3a", 0, id="linux-noninteractive"),
        pytest.param("Linux", "3.0", "n", "3.3a", 0, id="linux-manual-remediation"),
        pytest.param("Darwin", "3.3a", None, "3.3a", 0, id="darwin-supported"),
        pytest.param("Darwin", "3.0", None, "3.3a", 0, id="darwin-noninteractive"),
        pytest.param("Darwin", "3.0", "n", "3.3a", 0, id="darwin-declined"),
        pytest.param("Darwin", "3.0", "y", "3.3a", 0, id="darwin-upgraded"),
        pytest.param("Darwin", "3.0", "y", "3.0", 0, id="darwin-package-did-not-upgrade"),
        pytest.param("Darwin", "3.0", "y", "3.0", 17, id="darwin-package-failed"),
    ],
)
def test_installer_main_checks_tmux_with_explicit_consent(
    tmp_path: Path,
    platform: str,
    version: str,
    answer: str | None,
    upgrade_version: str,
    upgrade_status: int,
) -> None:
    """Drive the complete installer with a PTY and local executable dependencies."""
    home = tmp_path / "home"
    home.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "sh").symlink_to(SH)
    calls = tmp_path / "calls.jsonl"
    calls.touch()
    version_file = tmp_path / "tmux-version"
    version_file.write_text(version)
    dispatcher = tools / "fake-tool"
    dispatcher.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "tool, args = Path(sys.argv[0]).name, sys.argv[1:]\n"
        "with open(os.environ['TEST_CALLS'], 'a') as log:\n"
        "    log.write(json.dumps([tool, args]) + '\\n')\n"
        "if tool == 'uname':\n"
        "    print(os.environ['TEST_PLATFORM'])\n"
        "elif tool == 'tmux':\n"
        "    print('tmux ' + Path(os.environ['TEST_VERSION']).read_text())\n"
        "elif tool == 'brew' and args == ['list', '--formula', 'tmux']:\n"
        "    sys.exit(0)\n"
        "elif tool in ('sudo', 'brew'):\n"
        "    expected = ['apt-get', 'install', '-y', 'tmux'] if tool == 'sudo' "
        "else ['upgrade', 'tmux']\n"
        "    assert args == expected, args\n"
        "    Path(os.environ['TEST_VERSION']).write_text(os.environ['TEST_UPGRADE_VERSION'])\n"
        "    sys.exit(int(os.environ['TEST_UPGRADE_STATUS']))\n"
        "elif tool == 'uv' and args[:2] == ['tool', 'dir']:\n"
        "    print(os.environ['PATH'])\n"
        "elif tool == 'uv' and args[:2] == ['tool', 'install']:\n"
        "    assert args == ['tool', 'install', '--force', '-q', '--python', '3.12', 'omnigent']\n"
        "elif args == ['--version']:\n"
        "    print(tool + ' synthetic')\n"
    )
    dispatcher.chmod(0o755)
    for tool in ("uname", "tmux", "uv", "node", "npm", "bwrap", "apt-get", "sudo", "brew"):
        (tools / tool).symlink_to(dispatcher)
    for tool in ("omnigent", "omni"):
        executable = tools / tool
        executable.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m omnigent "$@"\n')
        executable.chmod(0o755)
    environment = {
        "HOME": str(home),
        "PATH": str(tools),
        "TERM": "dumb",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(
            str(INSTALLER.parents[1] / relative)
            for relative in (".", "sdks/python-client", "sdks/ui")
        ),
        "OMNIGENT_DATA_DIR": str(home / ".omnigent"),
        "TEST_CALLS": str(calls),
        "TEST_PLATFORM": platform,
        "TEST_VERSION": str(version_file),
        "TEST_UPGRADE_VERSION": upgrade_version,
        "TEST_UPGRADE_STATUS": str(upgrade_status),
    }
    command = [str(INSTALLER), "--no-databricks", "--verbose"]
    transcript = tmp_path / "installer.log"
    if answer is None:
        result = subprocess.run(
            [SH, *command, "--non-interactive"],
            env=environment,
            cwd=home,
            capture_output=True,
            text=True,
            timeout=90,
        )
        output = result.stdout + result.stderr
        transcript.write_text(output)
        status = result.returncode
    else:
        with transcript.open("w") as log:
            child = pexpect.spawn(SH, command, env=environment, cwd=str(home), encoding="utf-8")
            child.logfile = log
            try:
                if platform == "Darwin":
                    child.expect_exact("[Y/n] ", timeout=30)
                    assert "native terminals need tmux 3.3 or newer" in child.before
                    before_consent = [json.loads(line) for line in calls.read_text().splitlines()]
                    assert not [
                        call
                        for call in before_consent
                        if call[0] == "sudo" or (call[0] == "brew" and call[1][0] != "list")
                    ]
                    child.sendline(answer)
                child.expect(pexpect.EOF, timeout=90)
                child.close()
                status = child.exitstatus
            finally:
                child.close(force=True)
        output = transcript.read_text()
    assert status == 0, output
    assert "Omnigent installed successfully" in output
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    upgrades = [
        call
        for call in invocations
        if call[0] == "sudo" or (call[0] == "brew" and call[1][0] != "list")
    ]
    assert len(upgrades) == (1 if answer == "y" else 0)
    probes = [call for call in invocations if call[0] == "brew" and call[1][0] == "list"]
    assert probes == (
        [["brew", ["list", "--formula", "tmux"]]]
        if platform == "Darwin" and version == "3.0"
        else []
    )
    assert sum(call[0] == "uv" and call[1][:2] == ["tool", "install"] for call in invocations) == 1
    supported = version == "3.3a" or (answer == "y" and upgrade_version == "3.3a")
    assert ("tmux 3.3a is supported" in output) is supported
    if not supported:
        assert "tmux 3.3 or newer" in output
        assert "tmux -V" in output
        if platform == "Linux":
            assert "Install a supported version with your package manager" in output
            assert "[Y/n]" not in output
            assert "sudo apt-get" not in output
    if answer == "y" and not supported:
        assert "tmux 3.3 or newer is still unavailable" in output
    if answer == "y" and upgrade_status:
        assert "tmux install/upgrade failed" in output
    assert version_file.read_text() == (upgrade_version if answer == "y" else version)
    ledger = json.loads((home / ".omnigent" / "install_ledger.json").read_text())
    assert ledger["entries"]["deps"]["tmux"]["installed_by"] == "preexisting"


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


@pytest.mark.parametrize(
    ("version", "probe_status", "supported"),
    [
        ("tmux 3.0", 0, False),
        ("tmux 3.2a", 0, False),
        ("tmux 3.3", 0, True),
        ("tmux 3.3a", 0, True),
        ("tmux 3.10", 0, True),
        ("tmux 4.0", 0, True),
        ("unknown", 0, False),
        ("tmux 3.5", 1, False),
    ],
)
def test_check_tmux_validates_existing_version(
    lib: Path, version: str, probe_status: int, supported: bool
) -> None:
    """An installed binary is ready only when its version meets the runtime floor."""
    r = run(
        lib,
        f"""
        tmux() {{ printf '%s\\n' {shlex.quote(version)}; return {probe_status}; }}
        uname() {{ echo Linux; }}
        linux_pkg_install_cmd() {{ echo 'sudo apt-get install -y tmux'; }}
        sh() {{ echo unexpected-install; return 1; }}
        NON_INTERACTIVE=true
        check_tmux
        echo "ownership=$OMNIGENT_LEDGER_DEP_TMUX"
        """,
    )
    assert r.returncode == 0, r.stderr
    assert "ownership=preexisting" in r.stdout
    assert "unexpected-install" not in r.stdout
    if supported:
        assert not r.stderr
    else:
        assert "native terminals need tmux 3.3 or newer" in r.stderr
        assert "Install a supported version with your package manager" in r.stderr
        assert "sudo" not in r.stderr
        assert "tmux -V" in r.stderr
        assert "is supported" not in r.stdout


@pytest.mark.parametrize(
    ("platform", "preexisting", "brew_owned", "installed_version", "install_status"),
    [
        ("Darwin", True, True, "3.3a", 0),
        ("Darwin", True, False, "3.3a", 0),
        ("Darwin", False, False, "3.3a", 0),
        ("Darwin", True, True, "3.0", 0),
        ("Darwin", True, False, "3.0", 1),
        ("Linux", True, False, "3.3a", 0),
        ("Linux", False, False, "3.3a", 0),
        ("Linux", False, False, "3.0", 0),
        ("Linux", False, False, "3.0", 1),
    ],
)
def test_check_tmux_rechecks_install_or_upgrade(
    lib: Path,
    tmp_path: Path,
    platform: str,
    preexisting: bool,
    brew_owned: bool,
    installed_version: str,
    install_status: int,
) -> None:
    """Package-manager success does not guarantee a supported tmux on PATH."""
    bindir = _bindir(tmp_path)
    brew_calls = tmp_path / "brew-calls"
    brew_calls.touch()
    existing = 'tmux() { echo "tmux $tmux_test_version"; };' if preexisting else ""
    r = run(
        lib,
        f"""
        tmux_test_version=3.0
        {existing}
        uname() {{ echo {platform}; }}
        brew() {{
            printf '%s\\n' "$*" >> {shlex.quote(str(brew_calls))}
            return {0 if brew_owned else 1}
        }}
        linux_pkg_install_cmd() {{ echo 'sudo apt-get install -y tmux'; }}
        prompt_yes_no() {{ echo "offer:$1"; return 0; }}
        run_with_spinner() {{ shift; "$@"; }}
        sh() {{
            echo "install:$*"
            tmux_test_version={installed_version}
            tmux() {{ echo "tmux $tmux_test_version"; }}
            return {install_status}
        }}
        check_tmux
        echo "ownership=$OMNIGENT_LEDGER_DEP_TMUX"
        """,
        env={"PATH": bindir},
    )
    assert r.returncode == 0, r.stderr
    assert brew_calls.read_text() == (
        "list --formula tmux\n" if platform == "Darwin" and preexisting else ""
    )
    if platform == "Linux" and preexisting:
        assert "offer:" not in r.stdout
        assert "install:" not in r.stdout
        assert "ownership=preexisting" in r.stdout
        assert "native terminals need tmux 3.3 or newer" in r.stderr
        assert "Install a supported version with your package manager" in r.stderr
        assert "tmux -V" in r.stderr
        assert "sudo" not in r.stderr
        assert "is supported" not in r.stdout
        return
    command = (
        f"brew {'upgrade' if preexisting and brew_owned else 'install'} tmux"
        if platform == "Darwin"
        else "sudo apt-get install -y tmux"
    )
    assert f"install:-c {command}" in r.stdout
    assert "native terminals need tmux 3.3 or newer" in r.stdout
    assert f"ownership={'preexisting' if preexisting else 'omnigent'}" in r.stdout
    if installed_version == "3.3a":
        assert "tmux 3.3a is supported" in r.stdout
        assert not r.stderr
    else:
        assert "tmux 3.3 or newer is still unavailable" in r.stderr
        assert "tmux -V" in r.stderr
        assert "is supported" not in r.stdout
    if install_status:
        assert "tmux install/upgrade failed" in r.stderr


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


@pytest.mark.parametrize(
    ("args", "target"),
    [
        ("", "omnigent[databricks]"),
        ("--version 1.2.3", "omnigent[databricks]==1.2.3"),
        ("--repo https://github.com/o/r", "omnigent[databricks] @ git+https://github.com/o/r"),
        ("--extra databricks", "omnigent[databricks]"),
        ("--extra foo,databricks", "omnigent[foo,databricks]"),
        ("--extra foo,bar --extra baz", "omnigent[databricks,foo,bar,baz]"),
        ("--no-databricks", "omnigent"),
        ("--no-databricks --extra antigravity", "omnigent[antigravity]"),
        ("--extra antigravity --no-databricks", "omnigent[antigravity]"),
        ("--no-databricks --extra databricks", "omnigent[databricks]"),
    ],
)
def test_install_target_selects_extras(lib: Path, args: str, target: str) -> None:
    r = run(
        lib,
        f"""
        uv() {{ printf 'arg=%s\\n' "$@"; }}
        parse_args {args}
        normalize_repo_url
        install_omnigent
        """,
    )
    assert r.returncode == 0, r.stderr
    assert [line[4:] for line in r.stdout.splitlines() if line.startswith("arg=")] == [
        "tool",
        "install",
        "--force",
        "-q",
        "--python",
        "3.12",
        target,
    ]


_CLI_COMMANDS = """
uname() { case "$1" in -s) echo Linux;; -m) echo x86_64;; esac; }
uv() { [ "$1 $2" = 'python find' ] || return 1; printf '%s\\n' "$TEST_PYTHON"; }
DATABRICKS_CLI_SHA256_LINUX_AMD64="$TEST_ARCHIVE_SHA256"
DATABRICKS_CLI_SHA256_LINUX_ARM64="$TEST_ARCHIVE_SHA256"
DATABRICKS_CLI_SHA256_DARWIN_AMD64="$TEST_ARCHIVE_SHA256"
DATABRICKS_CLI_SHA256_DARWIN_ARM64="$TEST_ARCHIVE_SHA256"
curl() {
    printf '%s\\n' "$@" > "$TEST_CURL_LOG"
    cp "$TEST_ARCHIVE" "$4"
}
"""


@pytest.fixture
def databricks_env(tmp_path: Path) -> dict[str, str]:
    """Run the real shell/extraction path with a local release ZIP and isolated PATH."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for command in ("chmod", "cp", "mkdir", "mktemp", "mv", "rm"):
        executable = shutil.which(command)
        assert executable is not None
        (tools / command).symlink_to(executable)
    bindir = tmp_path / "tool bin"
    bindir.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    archive = tmp_path / "cli.zip"
    with zipfile.ZipFile(archive, "w") as release:
        release.writestr("databricks", '#!/bin/sh\n[ "$*" = "auth login --help" ]\n')
    return {
        "PATH": str(tools),
        "TMPDIR": str(scratch),
        "TEST_PYTHON": sys.executable,
        "TEST_ARCHIVE": str(archive),
        "TEST_ARCHIVE_SHA256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "TEST_EXECUTED": str(tmp_path / "executed"),
        "TEST_BIN_DIR": str(bindir),
        "TEST_CURL_LOG": str(tmp_path / "curl.log"),
        "TEST_LEDGER": str(tmp_path / "ledger.log"),
    }


@pytest.mark.parametrize(
    ("system", "arch", "release_platform"),
    [
        ("Linux", "x86_64", "linux_amd64"),
        ("Linux", "aarch64", "linux_arm64"),
        ("Darwin", "x86_64", "darwin_amd64"),
        ("Darwin", "arm64", "darwin_arm64"),
    ],
)
def test_install_databricks_cli(
    lib: Path, databricks_env: dict[str, str], system: str, arch: str, release_platform: str
) -> None:
    r = run(
        lib,
        _CLI_COMMANDS
        + f"""
        uname() {{ case "$1" in -s) echo {system};; -m) echo {arch};; esac; }}
        NON_INTERACTIVE=true
        ensure_databricks_cli "$TEST_BIN_DIR"
        "$TEST_BIN_DIR/databricks" auth login --help
        printf 'owner=%s\\n' "$OMNIGENT_LEDGER_DEP_DATABRICKS"
        """,
        env=databricks_env,
    )
    assert r.returncode == 0, r.stderr
    assert "owner=omnigent" in r.stdout
    curl_args = Path(databricks_env["TEST_CURL_LOG"]).read_text().splitlines()
    assert curl_args[:3] == [
        "-fsSL",
        "https://github.com/databricks/cli/releases/download/v1.21.0/"
        f"databricks_cli_1.21.0_{release_platform}.zip",
        "-o",
    ]
    assert list(Path(databricks_env["TMPDIR"]).iterdir()) == []


@pytest.mark.parametrize(
    ("location", "usable"),
    [("path", True), ("bin", True), ("path", False), ("bin", False), ("shadowed", False)],
)
def test_existing_databricks_cli_is_checked_without_replacement(
    lib: Path, databricks_env: dict[str, str], location: str, usable: bool
) -> None:
    tools = Path(databricks_env["PATH"])
    bindir = Path(databricks_env["TEST_BIN_DIR"])
    existing = (tools if location == "path" else bindir) / "databricks"
    contents = f"#!/bin/sh\nexit {0 if usable else 1}\n"
    existing.write_text(contents)
    existing.chmod(0o755)
    if location == "shadowed":
        global_cli = tools / "databricks"
        global_cli.write_text("#!/bin/sh\nexit 0\n")
        global_cli.chmod(0o755)
    r = run(lib, _CLI_COMMANDS + '\nensure_databricks_cli "$TEST_BIN_DIR"', env=databricks_env)
    assert (r.returncode == 0) == usable, r.stderr
    if not usable:
        assert "cannot run auth login" in r.stderr
        assert "--no-databricks" in r.stderr
    assert existing.read_text() == contents
    assert not Path(databricks_env["TEST_CURL_LOG"]).exists()


@pytest.mark.parametrize("failure", ["download", "checksum", "archive", "auth"])
def test_databricks_install_failure_does_not_publish_binary(
    lib: Path, databricks_env: dict[str, str], failure: str
) -> None:
    fault = ""
    if failure == "download":
        fault = "curl() { return 22; }"
    elif failure == "checksum":
        with zipfile.ZipFile(databricks_env["TEST_ARCHIVE"], "w") as release:
            release.writestr("databricks", '#!/bin/sh\nprintf ran > "$TEST_EXECUTED"\n')
    elif failure == "archive":
        Path(databricks_env["TEST_ARCHIVE"]).write_bytes(b"incomplete download")
    else:
        with zipfile.ZipFile(databricks_env["TEST_ARCHIVE"], "w") as release:
            release.writestr("databricks", "#!/bin/sh\nexit 1\n")
    if failure in {"archive", "auth"}:
        databricks_env["TEST_ARCHIVE_SHA256"] = hashlib.sha256(
            Path(databricks_env["TEST_ARCHIVE"]).read_bytes()
        ).hexdigest()
    r = run(
        lib,
        _CLI_COMMANDS + f'\n{fault}\nensure_databricks_cli "$TEST_BIN_DIR"',
        env=databricks_env,
    )
    assert r.returncode != 0
    assert "Databricks CLI installation failed" in r.stderr
    if failure == "checksum":
        assert "checksum mismatch" in r.stderr
        assert not Path(databricks_env["TEST_EXECUTED"]).exists()
    assert not (Path(databricks_env["TEST_BIN_DIR"]) / "databricks").exists()
    assert list(Path(databricks_env["TMPDIR"]).iterdir()) == []


@pytest.mark.parametrize(
    ("args", "download_fails"),
    [("", False), ("", True), ("--no-databricks --extra antigravity", False)],
)
def test_main_requires_selected_dependencies_before_reporting_success(
    lib: Path, databricks_env: dict[str, str], args: str, download_fails: bool
) -> None:
    bindir = Path(databricks_env["TEST_BIN_DIR"])
    omnigent = bindir / "omnigent"
    omnigent.write_text(
        '#!/bin/sh\nif [ "$*" = "_internal write-ledger --from-env" ]; then\n'
        '  command -v databricks > "$TEST_LEDGER"\n'
        '  printf "%s\\n" "$OMNIGENT_LEDGER_DEP_DATABRICKS" >> "$TEST_LEDGER"\n'
        "fi\n"
    )
    omnigent.chmod(0o755)
    (bindir / "omni").symlink_to(omnigent)
    fault = "curl() { return 22; }" if download_fails else ""
    r = run(
        lib,
        _CLI_COMMANDS
        + f"""
        uv() {{
            case "$1 $2" in
                'tool install') return 0;;
                'tool dir') printf '%s\\n' "$TEST_BIN_DIR";;
                'python find') printf '%s\\n' "$TEST_PYTHON";;
                *) return 1;;
            esac
        }}
        check_prerequisites() {{ :; }}
        check_node() {{ :; }}
        check_npm() {{ :; }}
        check_tmux() {{ :; }}
        check_bubblewrap() {{ :; }}
        {fault}
        main --non-interactive {args}
        """,
        env=databricks_env,
    )
    ledger = Path(databricks_env["TEST_LEDGER"])
    if download_fails:
        assert r.returncode != 0
        assert "installed successfully" not in r.stdout
        assert not ledger.exists()
    else:
        assert r.returncode == 0, r.stderr
        assert ledger.exists()
        if "--no-databricks" in args:
            assert not Path(databricks_env["TEST_CURL_LOG"]).exists()
            assert not (bindir / "databricks").exists()
            assert "Databricks" not in r.stdout
        else:
            assert ledger.read_text().splitlines() == [str(bindir / "databricks"), "omnigent"]
        assert "Omnigent installed successfully" in r.stdout
        assert f'PATH="{bindir}:$PATH"' in r.stdout
