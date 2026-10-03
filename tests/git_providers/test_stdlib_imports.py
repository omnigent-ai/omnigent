"""Git provider descriptors import only the standard library, and PR modules import cleanly."""

from __future__ import annotations

import itertools
import os
import subprocess
import sys

import pytest

from tests.budgets import budget

_PR_MODULES = (
    "omnigent.runner.session_prs",
    "omnigent.runner.github_resource",
    "omnigent.runner.pr_observer",
)


def _run_isolated(body: str) -> subprocess.CompletedProcess[str]:
    """Run *body* in a fresh ``python -I`` that resolves ``omnigent`` like this process.

    ``-I`` ignores ``PYTHONPATH``, so the probe adopts this process's import roots
    itself.
    """
    roots = [path for path in sys.path if path]
    probe = f"import sys\nsys.path[:0] = {roots!r}\n{body}"
    return subprocess.run(
        [sys.executable, "-I", "-c", probe],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=budget(120),
    )


def _allowed(name: str) -> bool:
    return (
        name.partition(".")[0] in sys.stdlib_module_names
        or name in {"omnigent", "omnigent._env_compat"}
        or name.startswith("omnigent.git_providers")
    )


def test_descriptors_import_only_the_standard_library() -> None:
    result = _run_isolated(
        "before = set(sys.modules)\n"
        "import omnigent.git_providers\n"
        "import omnigent.git_providers.github\n"
        "import omnigent.git_providers.gitlab\n"
        "omnigent.git_providers.providers()\n"
        "print('\\n'.join(sorted(set(sys.modules) - before)))\n"
    )

    assert result.returncode == 0, result.stderr
    loaded = result.stdout.split()
    assert "omnigent.git_providers.github" in loaded
    assert "omnigent.git_providers.gitlab" in loaded
    assert [name for name in loaded if not _allowed(name)] == []


@pytest.mark.parametrize(
    "order",
    list(itertools.permutations(_PR_MODULES)),
    ids=lambda order: ",".join(name.rsplit(".", 1)[-1] for name in order),
)
def test_pr_modules_import_in_any_order(order: tuple[str, ...]) -> None:
    result = _run_isolated(
        "".join(f"import {name}\n" for name in order)
        + "from omnigent.runner.session_prs import PullRequestRef\n"
        + "PullRequestRef.from_url('https://github.com/o/r/pull/1')\n"
    )

    assert result.returncode == 0, result.stderr
