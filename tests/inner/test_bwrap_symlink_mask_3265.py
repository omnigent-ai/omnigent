"""Escaping-symlink masks must not emit a mount onto the symlink path.

bwrap resolves a mount destination *through* a final symlink, so masking a
symlink aborts the whole namespace (``Can't create file at <link>`` for the
file shape, ``Can't mount tmpfs on <link>`` for the dir shape) and kills the
launcher at spawn. Skipping symlink entries is safe: the mount namespace
already confines symlink resolution, so the link is followed inside the
sandbox view where an escaping target is unmounted or separately masked.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

from omnigent.inner.bwrap_sandbox import _dotfile_and_symlink_mask_args
from omnigent.inner.sandbox import SandboxPolicy

_BWRAP = shutil.which("bwrap")


def _mask_args_for(tmp_path: pathlib.Path) -> list[str]:
    """Ask the real bwrap emitter for the mask args covering *tmp_path*."""
    policy = SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=[tmp_path],
        write_roots=[tmp_path],
        write_files=[],
        allow_network=True,
        cwd_hidden_scan_overflow="warn",
    )
    return _dotfile_and_symlink_mask_args(tmp_path, [], policy)


def test_no_mask_targets_a_symlink(tmp_path: pathlib.Path) -> None:
    """No emitted mount may point at a symlink, for either mask shape."""
    tasks = tmp_path / "proj" / "sess" / "tasks"
    tasks.mkdir(parents=True)
    # Escape to $HOME: /tmp is itself a safe root, so a tmp-to-tmp link never
    # counts as escaping. This mirrors production, where the claude CLI links
    # /tmp/claude-<uid>/.../tasks/<id>.output into ~/.claude/projects/.
    escaping_file = pathlib.Path.home() / ".claude" / "nonexistent-3265.jsonl"
    escaping_dir = pathlib.Path.home() / ".claude"
    # A symlink to a file (the `tasks/<id>.output` shape) and one to a
    # directory — the dir shape aborts too, via `--tmpfs <link>`.
    (tasks / "abc.output").symlink_to(escaping_file)
    (tasks / "dirlink").symlink_to(escaping_dir)

    args = _mask_args_for(tmp_path)
    destinations = [
        args[i + 2] if args[i] != "--tmpfs" else args[i + 1]
        for i in range(len(args))
        if args[i] in ("--tmpfs", "--bind-try")
    ]
    offenders = [d for d in destinations if pathlib.Path(d).is_symlink()]
    assert not offenders, f"mask targets a symlink, which aborts bwrap: {offenders}"


@pytest.mark.skipif(_BWRAP is None, reason="bwrap not installed")
def test_skipping_symlink_masks_does_not_leak_the_target(tmp_path: pathlib.Path) -> None:
    """Skipping the symlink mask must not expose a masked target's content."""
    secret = tmp_path / ".env"
    secret.write_text("DOTSECRET", encoding="utf-8")
    dotdir = tmp_path / ".aws"
    dotdir.mkdir()
    (dotdir / "credentials").write_text("AWSKEY", encoding="utf-8")

    # Absolute links to the masked paths: the mask must still win when the
    # sandbox follows them, even though no mount is emitted on the links.
    (tmp_path / "link_to_dotfile").symlink_to(secret.resolve())
    (tmp_path / "link_into_dotdir").symlink_to((dotdir / "credentials").resolve())

    argv = [_BWRAP, "--ro-bind", "/usr", "/usr"]
    for path in ("/bin", "/lib", "/lib64"):
        if pathlib.Path(path).exists():
            argv += ["--ro-bind-try", path, path]
    argv += ["--dev", "/dev", "--tmpfs", "/tmp"]
    argv += ["--bind-try", str(tmp_path), str(tmp_path)]
    # Exactly what the emitter now produces: masks on the real dotfile and
    # dotdir, and nothing at all on the two symlinks.
    argv += _mask_args_for(tmp_path)
    argv += [
        shutil.which("sh") or "/bin/sh",
        "-c",
        f"cat {tmp_path / 'link_to_dotfile'} {tmp_path / 'link_into_dotdir'} {secret} "
        "2>/dev/null; exit 0",
    ]

    done = subprocess.run(argv, capture_output=True, text=True)
    assert done.returncode == 0, f"namespace must assemble: {done.stderr}"
    assert "DOTSECRET" not in done.stdout
    assert "AWSKEY" not in done.stdout


@pytest.mark.skipif(_BWRAP is None, reason="bwrap not installed")
def test_bwrap_rejects_a_bind_onto_a_symlink(tmp_path: pathlib.Path) -> None:
    """Pin the bwrap behavior the emitter has to respect."""
    plain = tmp_path / "plain"
    plain.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(plain)

    base = [_BWRAP, "--ro-bind", "/usr", "/usr"]
    for path in ("/bin", "/lib", "/lib64"):
        if pathlib.Path(path).exists():
            base += ["--ro-bind-try", path, path]
    base += [
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--bind-try",
        str(tmp_path),
        str(tmp_path),
    ]
    true_bin = shutil.which("true") or "/bin/true"

    ok = subprocess.run(
        [*base, "--bind-try", "/dev/null", str(plain), true_bin],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, f"regular-file mask should work: {ok.stderr}"

    bad = subprocess.run(
        [*base, "--bind-try", "/dev/null", str(link), true_bin],
        capture_output=True,
        text=True,
    )
    assert bad.returncode != 0
    assert "Can't create file at" in bad.stderr
