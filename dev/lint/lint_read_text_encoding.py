"""Flag ``Path.read_text()`` calls in the shipped package that omit ``encoding``.

What goes wrong
---------------

``Path.read_text()`` with no ``encoding`` argument decodes using the
*locale* encoding. On POSIX that is effectively always UTF-8, so the
omission is invisible in CI and on maintainer machines. On Windows it
is the ANSI codepage — ``cp1252`` for a Western install — and the same
call then reads a UTF-8 file as if it were single-byte Latin-1::

    em dash  U+2014  ->  UTF-8 bytes E2 80 94  ->  cp1252 gives 'a EUR "'

Only five byte values (``0x81 0x8D 0x8F 0x90 0x9D``) are undefined in
cp1252, so roughly 98% of mis-decoded bytes produce *no error at all* —
they silently become the wrong characters. A file crashes only if its
non-ASCII content happens to include one of those five. That makes the
loud failures the lucky minority and silent corruption the norm: the
bundled ``examples/polly/config.yaml`` carries 189 non-ASCII bytes,
none of which land on a hole, so its system prompt loaded corrupted on
Windows without raising anything.

Every file a *user* authors is exposed: agent ``config.yaml``,
``SKILL.md``, ``AGENTS.md`` / ``CLAUDE.md``, ``tools/mcp/*.yaml``, and
the third-party tool configs Omnigent inspects (``uv.toml``,
``~/.qwen/settings.json``). Naming the encoding makes the behaviour
identical on every platform.

What this catches
-----------------

``<expr>.read_text()`` anywhere under ``omnigent/`` with no
``encoding=`` keyword argument.

What this does NOT catch
------------------------

- ``write_text()``. It has the identical defect on the write side and
  there are ~36 bare call sites in the package; folding them in here
  would widen a focused fix into a package-wide sweep. Deliberate
  follow-up, not an oversight.
- Bare ``open()`` / ``io.open()``, for the same reason.
- Reads outside ``omnigent/`` (``tests/``, ``dev/``, ``scripts/``,
  ``.github/``). Those never run on a user's machine, so they cannot
  produce this bug in an install.

Allowed exceptions
------------------

``ALLOWED`` lists reads of files Omnigent writes for itself — pid
files, caches, ledgers, generated secrets, daemon records. Their
content is digits, hashes, and ASCII identifiers, so the locale
encoding cannot corrupt them. Each entry is keyed by enclosing
function rather than line number so it survives unrelated edits. A new
entry should be added only when the file being read is machine-written;
if a human can type into it, add ``encoding="utf-8"`` instead.

Exit code 1 if any hits are found.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# (repo-relative path, enclosing function) pairs whose read targets are
# written by Omnigent itself and can never contain non-ASCII text.
ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        ("omnigent/cli.py", "_migrate_legacy_state_dir"),
        ("omnigent/cli.py", "_read_daemon_record"),
        ("omnigent/cli.py", "_read_host_pid_file"),
        ("omnigent/cli_auth.py", "_store_entry"),
        ("omnigent/cli_auth.py", "_load_entry"),
        ("omnigent/cli_auth.py", "clear_token"),
        ("omnigent/host/local_server.py", "_read_local_server_pid_file"),
        ("omnigent/host/local_server.py", "_read_local_server_log_path"),
        ("omnigent/host/local_server.py", "_read_local_server_sig"),
        ("omnigent/install_ledger.py", "load_ledger"),
        ("omnigent/install_ledger.py", "read_installation_id"),
        ("omnigent/install_ledger.py", "find_profile_block"),
        ("omnigent/integration_daemon.py", "read_record"),
        ("omnigent/llms/_usage_observer.py", "_current_test_from_sidecar"),
        ("omnigent/onboarding/ucode_state.py", "read_ucode_state"),
        ("omnigent/onboarding/ucode_state.py", "read_current_ucode_state"),
        ("omnigent/runner/identity.py", "load_or_create_runner_id"),
        ("omnigent/server/accounts_secret.py", "load_or_generate_cookie_secret"),
        ("omnigent/update_check.py", "_read_cache"),
        ("omnigent/update_check.py", "_read_uv_tool_extras"),
        ("omnigent/update_check.py", "_read_pipx_extras"),
    }
)


def _repo_relative(path: Path) -> str | None:
    """
    Return ``path`` as a repo-relative POSIX string under ``omnigent/``.

    :param path: File path as handed over by pre-commit (relative) or a
        developer (possibly absolute).
    :returns: e.g. ``"omnigent/spec/parser.py"``, or ``None`` when the
        path is not inside the shipped package.
    """
    parts = path.as_posix().split("/")
    if "omnigent" not in parts:
        return None
    # Last occurrence handles an absolute path into a clone that is
    # itself named "omnigent" (…/projects/omnigent/omnigent/spec/…).
    idx = len(parts) - 1 - parts[::-1].index("omnigent")
    return "/".join(parts[idx:])


def _function_owners(tree: ast.Module) -> list[tuple[int, int, str]]:
    """
    Return ``[(start, end, name), ...]`` for every function in ``tree``.

    :param tree: Parsed module.
    :returns: One span per function; innermost wins when spans nest,
        resolved by the caller taking the largest ``start``.
    """
    spans: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.append((node.lineno, node.end_lineno or node.lineno, node.name))
    return spans


def _enclosing(spans: list[tuple[int, int, str]], lineno: int) -> str:
    """
    Return the innermost function name containing ``lineno``.

    :param spans: Output of :func:`_function_owners`.
    :param lineno: Line to locate.
    :returns: Function name, or ``"<module>"`` at module scope.
    """
    best = ("<module>", -1)
    for start, end, name in spans:
        if start <= lineno <= end and start > best[1]:
            best = (name, start)
    return best[0]


def scan(path: Path) -> list[tuple[int, str]]:
    """
    Return ``[(lineno, message), ...]`` for each bare ``read_text()``.

    :param path: File to scan. Ignored unless it sits under ``omnigent/``.
    :returns: Hits with line numbers and descriptive messages.
    """
    rel = _repo_relative(path)
    if rel is None:
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []

    spans = _function_owners(tree)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "read_text":
            continue
        if node.args:
            # ``Path.read_text`` takes no positional arguments, so a
            # positional call is a different API that merely shares the
            # name — notably ``importlib.metadata.Distribution.read_text``,
            # which takes a filename and always decodes as UTF-8.
            continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        owner = _enclosing(spans, node.lineno)
        if (rel, owner) in ALLOWED:
            continue
        hits.append(
            (
                node.lineno,
                f"`read_text()` in `{owner}` has no `encoding=`: decodes with the "
                "locale codepage, corrupting non-ASCII on Windows",
            ),
        )
    return hits


def main(argv: list[str]) -> int:
    """
    Scan every file in ``argv[1:]`` and report bare ``read_text()`` calls.

    :param argv: Command-line args (``argv[0]`` is the script name).
    :returns: Exit code — 0 on clean scan, 1 if any hits.
    """
    failed = False
    for arg in argv[1:]:
        path = Path(arg)
        if not path.is_file():
            continue
        hits = scan(path)
        if hits:
            failed = True
            for line, msg in hits:
                sys.stdout.write(f"{path}:{line}: {msg}\n")
    if failed:
        sys.stdout.write(
            '\nPass `encoding="utf-8"` explicitly. Without it Python decodes '
            "using the locale encoding, which is UTF-8 on POSIX but the ANSI "
            "codepage on Windows — so the same file reads correctly in CI and "
            "corrupts on a user's machine, usually with no error raised. If "
            "the file is written by Omnigent itself and can only ever contain "
            "ASCII, add its (path, function) to ALLOWED in this script with a "
            "note saying why.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
