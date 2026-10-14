"""E2E (no LLM): ``omnigent speculate`` exit statuses, promotions, early stops and signals.

Each scripted replica fixes one of two files and exits non-zero, so neither passes alone
and no replica exit status can be trusted. The CLI process must compose them, verify the
candidate outside both worktrees, and print the ledger as JSON.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

import pytest

_CHECK = "import a, b, sys\nprint('A', a.A, 'B', b.B)\nsys.exit((a.A, b.B) != (1, 1))\n"


@pytest.fixture
def env(omnigent_repo_root: Path, tmp_path: Path) -> dict[str, str]:
    roles = {f"GIT_{r}_{k}": "t" for r in ("AUTHOR", "COMMITTER") for k in ("NAME", "EMAIL")}
    return {
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(omnigent_repo_root),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        **roles,
    }


@pytest.fixture
def repo(tmp_path: Path, env: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, text in {"a.py": "A = 0\n", "b.py": "B = 0\n", "check.py": _CHECK}.items():
        (repo / name).write_text(text)
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "initial"]):
        _git(repo, env, *args)
    return repo


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)
    return done.stdout.decode().strip()


def _speculate(python: Path, *replicas: str, protect: str = "check.py") -> list[str]:
    """``omnigent speculate`` argv with one replica per Python snippet."""
    argv = [str(python), "-m", "omnigent", "speculate", "make check.py pass"]
    for code in replicas:
        argv += ["--replica", shlex.join([str(python), "-c", code])]
    verify = shlex.join([str(python), "check.py"])
    return [*argv, "-k", str(len(replicas)), "--protect", protect, "--verify", verify]


def test_speculate_exit_status_and_composed_promotion(
    omnigent_python: Path, repo: Path, env: dict[str, str]
) -> None:
    """Exit 2 on a bad protected path, 1 when nothing passes, else 0 with the composition."""
    fix_a, fix_b = (
        f"import pathlib; pathlib.Path({name!r}).write_text({text!r}); exit(3)"
        for name, text in (("a.py", "A = 1\n"), ("b.py", "B = 1\n"))
    )

    def speculate(protect: str) -> subprocess.CompletedProcess[bytes]:
        argv = _speculate(omnigent_python, fix_a, fix_b, protect=protect)
        return subprocess.run(argv, cwd=repo, env=env, capture_output=True, timeout=300)

    misconfigured, unpromoted, run = speculate("chek.py"), speculate("a.py"), speculate("check.py")

    assert (misconfigured.returncode, misconfigured.stdout) == (2, b"")
    assert b"'chek.py' matches no file in the checkpoint" in misconfigured.stderr
    assert (unpromoted.returncode, json.loads(unpromoted.stdout)["branch"]) == (1, None)
    assert run.returncode == 0, run.stderr.decode()
    assert run.stderr.decode().count("exited with status 3") == 2
    report = json.loads(run.stdout)
    assert report["composed_only_win"] is True
    ledger = sorted((entry["status"], entry["attempts"]) for entry in report["ledger"])
    assert ledger == [("failed", []), ("failed", ["r1@1"]), ("failed", ["r2@1"]), ("passed", [])]
    assert report["promoted"] == report["ledger"][-1]["tree"]
    assert _git(repo, env, "show", f"{report['commit']}:a.py") == "A = 1"
    assert _git(repo, env, "show", f"{report['commit']}:b.py") == "B = 1"
    _git(repo, env, "merge", "-q", "--ff-only", report["branch"])
    already = speculate("check.py")
    assert (already.returncode, json.loads(already.stdout)["admitted"]) == (0, False)


def test_first_passing_replica_wins_and_the_slow_one_is_killed(
    omnigent_python: Path, repo: Path, env: dict[str, str], tmp_path: Path
) -> None:
    """r2 passes once r1 has started, so the CLI kills r1, a 60 s sleep, instead of waiting."""
    pid = tmp_path / "pid"
    slow = (
        f"import os, pathlib, time; pathlib.Path({str(pid)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)"
    )
    fix = (
        f"import pathlib, time\nwhile not pathlib.Path({str(pid)!r}).exists(): time.sleep(0.01)\n"
        "pathlib.Path('a.py').write_text('A = 1\\n'); pathlib.Path('b.py').write_text('B = 1\\n')"
    )
    argv = _speculate(omnigent_python, slow, fix)
    start = time.monotonic()

    run = subprocess.run(argv, cwd=repo, env=env, capture_output=True, timeout=120)

    assert run.returncode == 0, run.stderr.decode()
    assert time.monotonic() - start < 20
    assert _exits(int(pid.read_text()))


@pytest.mark.parametrize(
    "sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP], ids=lambda sig: sig.name
)
def test_signal_kills_the_replicas_and_exits_128_plus_the_signal(
    omnigent_python: Path, repo: Path, env: dict[str, str], tmp_path: Path, sig: signal.Signals
) -> None:
    """A replica runs in its own session, so only the CLI can stop it from editing."""
    pid, beat, err = tmp_path / "pid", tmp_path / "beat", tmp_path / "stderr"
    writer = (
        f"import os, pathlib, time; pathlib.Path({str(pid)!r}).write_text(str(os.getpid()))\n"
        "for i in range(3000):\n    try: pathlib.Path(f'f{i}').touch()\n    except OSError: pass\n"
        f"    pathlib.Path({str(beat)!r}).write_text(str(i)); time.sleep(0.01)"
    )
    with err.open("wb") as stderr:
        cli = subprocess.Popen(
            _speculate(omnigent_python, writer), cwd=repo, env=env, stderr=stderr
        )
    deadline = time.monotonic() + 60
    while not beat.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    cli.send_signal(sig)

    assert cli.wait(10) == 128 + sig, err.read_text()[-2000:]
    assert _exits(int(pid.read_text()))
    last = beat.read_text()
    time.sleep(0.2)
    assert beat.read_text() == last
    assert "omnigent-speculate-" not in _git(repo, env, "worktree", "list")


def _exits(pid: int) -> bool:
    """Whether process ``pid`` is gone within 2 s."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False
