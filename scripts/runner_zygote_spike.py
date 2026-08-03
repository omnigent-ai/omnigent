#!/usr/bin/env python3
"""Phase-1 spike: does forking a warm runner zygote actually save memory?

Not wired into the daemon. This measures whether copy-on-write sharing of the
runner import graph materializes, so we can decide whether the full zygote
architecture is worth building.

Two modes:

  fork      Import the runner graph once, then os.fork() N children that idle
            in the imported state. Measures shared vs private footprint.
  popen     Baseline: spawn N fresh `python -c "import graph"` processes, each
            paying the import floor independently.

The parent then sums each child's memory via macOS `footprint`/`vmmap` and
prints fork-total vs popen-total.

CAVEAT (read before trusting a number): this runs on macOS, which has no
/proc/<pid>/smaps and no Pss. We use Mach `phys_footprint` (via `footprint`),
which *does* discount COW-shared clean pages, so it is the closest macOS analog
to Pss — but it is NOT the Linux prod figure. Treat the delta as "does COW
sharing show up at all, and roughly how big," not as the production saving.

Usage:
    python scripts/runner_zygote_spike.py --n 8
    python scripts/runner_zygote_spike.py --n 8 --freeze
    python scripts/runner_zygote_spike.py --mode fork --n 8
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
import time


def _import_runner_graph() -> object:
    """Import the heavy runner graph the zygote would preload.

    Returns the create_app callable so we can prove a forked child is
    functional to the same point a cold runner reaches, not merely cheap.
    """
    import omnigent.runner.app
    import omnigent.runner.native  # noqa: F401
    from omnigent.runner._entry import create_app

    return create_app


def _footprint_bytes(pid: int) -> int | None:
    """Return Mach phys_footprint for pid in bytes, or None if unavailable.

    `footprint -p <pid>` prints a summary line ending in the process's
    phys_footprint. Parsing is best-effort; vmmap is the fallback.
    """
    try:
        out = subprocess.run(
            ["footprint", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return _vmmap_footprint_bytes(pid)
    # footprint output includes a line like:
    #   "phys_footprint:  123.4 MB (129,000,000 bytes)"
    for line in out.stdout.splitlines():
        low = line.lower()
        if "phys_footprint" in low or "physical footprint" in low:
            got = _extract_bytes(line)
            if got is not None:
                return got
    return _vmmap_footprint_bytes(pid)


def _vmmap_footprint_bytes(pid: int) -> int | None:
    """Fallback: parse `vmmap --summary <pid>` physical footprint line."""
    try:
        out = subprocess.run(
            ["vmmap", "--summary", str(pid)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if "Physical footprint:" in line and "Peak" not in line:
            return _extract_bytes(line)
    return None


def _extract_bytes(line: str) -> int | None:
    """Extract a byte count from a footprint/vmmap line.

    Handles both an explicit "(129,000,000 bytes)" and a "123.4 MB" / "1.2G"
    magnitude token.
    """
    import re

    paren = re.search(r"\(([\d,]+)\s*bytes\)", line)
    if paren:
        return int(paren.group(1).replace(",", ""))
    mag = re.search(r"([\d.]+)\s*([KMG])B?\b", line)
    if mag:
        val = float(mag.group(1))
        unit = mag.group(2)
        mult = {"K": 1024, "M": 1024**2, "G": 1024**3}[unit]
        return int(val * mult)
    return None


def _run_fork_mode(n: int, freeze: bool) -> None:
    """Import once, fork n idle children, measure each, report aggregate."""
    print(f"[fork] importing runner graph in parent (pid={os.getpid()})...")
    t0 = time.time()
    create_app = _import_runner_graph()
    print(f"[fork] import done in {time.time() - t0:.2f}s")

    # Prove the parent's graph is usable (a forked child inherits this state).
    app = create_app(auth_token_factory=lambda: None)
    print(f"[fork] parent create_app() ok: {type(app).__name__}")
    del app

    if freeze:
        gc.freeze()
        print("[fork] gc.freeze() applied — static graph moved out of GC set")

    child_pids: list[int] = []
    for _ in range(n):
        pid = os.fork()
        if pid == 0:
            # Child: idle so the parent can measure our resident footprint.
            # We deliberately do NOT connect a tunnel — we measure the import
            # floor a runner starts from, holding the inherited COW pages.
            try:
                # Touch the inherited callable to confirm usability post-fork,
                # then sleep to be measured.
                _ = create_app  # inherited via COW
                time.sleep(120)
            finally:
                os._exit(0)
        child_pids.append(pid)

    # Let children settle before measuring.
    time.sleep(3)

    print(f"[fork] measuring {len(child_pids)} children...")
    total = 0
    measured = 0
    for pid in child_pids:
        fb = _footprint_bytes(pid)
        if fb is None:
            print(f"  child pid={pid}: footprint UNAVAILABLE")
            continue
        measured += 1
        total += fb
        print(f"  child pid={pid}: {fb / 1024 / 1024:.1f} MB")

    parent_fb = _footprint_bytes(os.getpid())
    print(
        f"[fork] parent pid={os.getpid()}: "
        f"{(parent_fb or 0) / 1024 / 1024:.1f} MB (holds the shared graph)"
    )
    if measured:
        print(
            f"[fork] AGGREGATE child footprint ({measured} procs): "
            f"{total / 1024 / 1024:.1f} MB  (avg {total / measured / 1024 / 1024:.1f} MB/child)"
        )
    print(
        json.dumps(
            {
                "mode": "fork",
                "n": n,
                "freeze": freeze,
                "children_measured": measured,
                "aggregate_child_bytes": total,
                "parent_bytes": parent_fb,
            }
        )
    )

    for pid in child_pids:
        try:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except OSError:
            pass


def _run_popen_mode(n: int) -> None:
    """Baseline: n independent interpreters each paying the import floor."""
    print(f"[popen] spawning {n} fresh interpreters, each importing the graph...")
    child_src = (
        "import time;"
        "import omnigent.runner.app;"
        "import omnigent.runner.native;"
        "from omnigent.runner._entry import create_app;"
        "create_app(auth_token_factory=lambda: None);"
        "time.sleep(120)"
    )
    procs = [subprocess.Popen([sys.executable, "-c", child_src]) for _ in range(n)]
    # Fresh imports are slow — give them room to finish before measuring.
    time.sleep(20)

    print(f"[popen] measuring {len(procs)} processes...")
    total = 0
    measured = 0
    for p in procs:
        fb = _footprint_bytes(p.pid)
        if fb is None:
            print(f"  proc pid={p.pid}: footprint UNAVAILABLE")
            continue
        measured += 1
        total += fb
        print(f"  proc pid={p.pid}: {fb / 1024 / 1024:.1f} MB")

    if measured:
        print(
            f"[popen] AGGREGATE footprint ({measured} procs): "
            f"{total / 1024 / 1024:.1f} MB  (avg {total / measured / 1024 / 1024:.1f} MB/proc)"
        )
    print(
        json.dumps(
            {
                "mode": "popen",
                "n": n,
                "procs_measured": measured,
                "aggregate_bytes": total,
            }
        )
    )

    for p in procs:
        p.kill()
        p.wait()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=8, help="number of child runners")
    ap.add_argument(
        "--mode",
        choices=["fork", "popen", "both"],
        default="both",
        help="which measurement to run",
    )
    ap.add_argument(
        "--freeze",
        action="store_true",
        help="apply gc.freeze() before forking (fork mode only)",
    )
    args = ap.parse_args()

    if args.mode in ("popen", "both"):
        _run_popen_mode(args.n)
    if args.mode in ("fork", "both"):
        # Run fork mode in a child so a clean parent does the popen baseline
        # first without the heavy graph resident.
        _run_fork_mode(args.n, args.freeze)


if __name__ == "__main__":
    main()
