"""Typed filesystem service wrapping legacy OSEnvironment methods.

Translates between the typed ``EnvironmentFilesystem`` protocol
(``FilesystemEntry``, ``FileContent``, ``WriteFileResult``, etc.)
and the opaque ``OpResult`` dicts returned by the existing
``OSEnvironment.read/write/edit/shell`` methods.

See ``designs/SESSION_RESOURCES_API_DESIGN.md`` §Environment
filesystem service.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnigent.entities.environment_filesystem import (
    DeleteFilesystemResult,
    DirectoryNotEmpty,
    EditFileResult,
    FileContent,
    FilesystemEntry,
    FilesystemPathNotFound,
    InvalidPath,
    TextEditRequest,
    WriteFileResult,
)
from omnigent.entities.pagination import PagedList
from omnigent.inner.os_env import _DEFAULT_READ_LIMIT

if TYPE_CHECKING:
    from omnigent.inner.os_env import OSEnvironment

_MAX_READ_BYTES = 10 * 1024 * 1024  # 10 MiB


def _shell_quote(s: str) -> str:
    """Shell-quote a string for safe interpolation.

    :param s: The string to quote.
    :returns: Single-quoted shell-safe string.
    """
    return "'" + s.replace("'", "'\\''") + "'"


def _glob_to_regex(pattern: str) -> str:
    """Translate a VSCode/Cursor-style glob into an anchored regex over a path.

    The returned regex fully matches a forward-slash-separated relative path
    (e.g. ``"src/app/main.ts"``).  The supported syntax mirrors the common
    subset accepted by the VSCode/Cursor "files to include" / "files to
    exclude" boxes:

    - ``*`` matches any run of characters except ``/`` (one path segment).
    - ``**`` matches any run of characters including ``/`` (cross-directory).
    - ``?`` matches a single character except ``/``.
    - ``{a,b,c}`` matches any one of the comma-separated alternatives. The
      alternatives are treated *literally* — use ``"*.{js,ts}"`` rather than
      ``"{*.js,*.ts}"``.
    - A pattern containing no ``/`` matches the *basename* at any depth (an
      implicit ``**/`` prefix), so ``"*.ts"`` matches ``"src/a.ts"``.
    - A pattern ending in ``/`` matches that directory and everything below
      it, so ``"build/"`` is treated as ``"build/**"``.

    Every character outside the supported metacharacters is passed through
    ``re.escape``, so a user-supplied pattern can never inject arbitrary
    regex.  ``**`` expands to ``.*`` and ``*`` to ``[^/]*`` — both linear,
    so the result is not vulnerable to catastrophic backtracking.

    :param pattern: A single glob pattern, already stripped of surrounding
        whitespace, e.g. ``"src/**/*.ts"`` or ``"*.test.ts"``.
    :returns: An anchored regex string (``^...$``) suitable for
        ``re.compile(..., re.IGNORECASE)``.
    """
    # Basename-only patterns match at any depth; trailing-slash patterns
    # match the whole subtree.
    if "/" not in pattern:
        pattern = "**/" + pattern
    elif pattern.endswith("/"):
        pattern = pattern + "**"

    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # "**/" consumes the trailing slash so it can also match zero
                # path segments (e.g. "**/foo" matches a top-level "foo").
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "{":
            close = pattern.find("}", i)
            if close == -1:
                # Unbalanced brace — match it literally.
                out.append(re.escape(c))
                i += 1
            else:
                alts = pattern[i + 1 : close].split(",")
                out.append("(?:" + "|".join(re.escape(a) for a in alts) + ")")
                i = close + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "^" + "".join(out) + "$"


def split_glob_list(raw: str | None) -> list[str]:
    """Split a comma-separated glob list on top-level commas only.

    Commas inside a ``{...}`` brace alternation are preserved, so a value like
    ``"*.{js,ts}, src/**"`` splits into ``["*.{js,ts}", "src/**"]`` rather than
    tearing the brace group apart on its inner comma. Blank entries (e.g. from
    a trailing comma) are dropped and the rest are stripped of surrounding
    whitespace. This is the single layer responsible for parsing/cleaning glob
    lists; downstream consumers receive ready-to-translate patterns.

    :param raw: Raw comma-separated patterns from a query param, e.g.
        ``"*.{js,ts}, src/**"``. ``None`` or an empty string yields an empty
        list (the param is absent / carries no patterns).
    :returns: List of individual non-empty glob patterns.
    """
    if not raw:
        return []
    patterns: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in raw:
        if ch == "{":
            depth += 1
            current.append(ch)
        elif ch == "}":
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == "," and depth == 0:
            patterns.append("".join(current))
            current = []
        else:
            current.append(ch)
    patterns.append("".join(current))
    return [p.strip() for p in patterns if p.strip()]


async def _run_os_env_async(
    method: Any,
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Call an OSEnvironment async method.

    The OSEnvironment protocol uses ``OpResult = dict[str, Any]``
    for all return types. ``Any`` is intentional here — the helper
    wraps methods with varying signatures and the dict values are
    heterogeneous.

    :param method: The async OSEnvironment method.
    :param args: Positional arguments.
    :param kwargs: Keyword arguments.
    :returns: The OpResult dict.
    """
    return await method(*args, **kwargs)  # type: ignore[no-any-return]


def _validate_path(relative_path: str) -> str:
    """Validate and normalize a relative path.

    Rejects NUL bytes, absolute paths, and traversal attempts.

    :param relative_path: Client-supplied path string.
    :returns: Normalized relative path.
    :raises InvalidPath: On validation failure.
    """
    if "\x00" in relative_path:
        raise InvalidPath("Path contains NUL bytes")
    normalized = os.path.normpath(relative_path)
    if os.path.isabs(normalized):
        raise InvalidPath("Absolute paths are not allowed")
    if normalized.startswith(".."):
        raise InvalidPath("Path traversal is not allowed")
    if normalized == ".":
        return ""
    return normalized


def _entry_from_stat(
    _root: Path,
    full_path: Path,
    relative: str,
) -> FilesystemEntry:
    """Build a FilesystemEntry from a real filesystem path.

    :param root: Environment root directory.
    :param full_path: Absolute path to the entry.
    :param relative: Path relative to root.
    :returns: The filesystem entry.
    """
    try:
        st = full_path.stat()
    except OSError:
        return FilesystemEntry(
            id=relative or full_path.name,
            name=full_path.name,
            path=relative,
            type="other",
        )

    if stat.S_ISDIR(st.st_mode):
        entry_type = "directory"
        size = None
    elif stat.S_ISLNK(st.st_mode):
        entry_type = "symlink"
        size = st.st_size
    elif stat.S_ISREG(st.st_mode):
        entry_type = "file"
        size = st.st_size
    else:
        entry_type = "other"
        size = None

    return FilesystemEntry(
        id=relative or full_path.name,
        name=full_path.name,
        path=relative,
        type=entry_type,
        bytes=size,
        modified_at=int(st.st_mtime),
    )


class CallerProcessFilesystem:
    """Filesystem service backed by the caller-process OSEnvironment.

    Routes read/write/edit through the OSEnvironment's methods so
    sandbox policies (bwrap, seatbelt) are enforced by the helper
    subprocess.  Directory listing and stat use ``os_env.shell()``
    for the same reason.  The ``_root`` path is used only for
    path validation (traversal checks), not for direct I/O.

    :param os_env: The backing OSEnvironment instance.
    """

    def __init__(self, os_env: OSEnvironment) -> None:
        self._os_env = os_env
        self._root = Path(os_env.cwd).resolve()

    def _resolve(self, path: str) -> Path:
        """Resolve a relative path to an absolute path under root.

        :param path: Relative path within the environment.
        :returns: Resolved absolute path.
        :raises InvalidPath: If path escapes the root.
        """
        validated = _validate_path(path)
        if not validated:
            return self._root
        full = (self._root / validated).resolve()
        try:
            full.relative_to(self._root)
        except ValueError as exc:
            raise InvalidPath(f"Path {path!r} escapes the environment root") from exc
        return full

    async def list_dir(
        self,
        path: str = "",
        *,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> PagedList[FilesystemEntry]:
        """List directory contents via the sandboxed helper.

        Uses ``os_env.shell()`` to run a Python script inside the sandbox so
        access control is enforced.  The script uses ``os.lstat()`` (does not
        follow symlinks) for size/mtime and ``os.path.isdir()`` (follows
        symlinks but returns ``False`` for broken ones) for type classification.
        Per-entry ``OSError`` is silently skipped so a single inaccessible
        entry (e.g. a broken Bazel symlink in a large monorepo) does not cause
        the entire listing to fail.

        :param path: Relative directory path. Empty string or
            ``"."`` for the root.
        :param limit: Maximum entries to return.
        :param after: Cursor entry id for forward pagination.
        :param before: Cursor entry id for backward pagination.
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :returns: Paginated list of filesystem entries.
        :raises FilesystemPathNotFound: If the directory does not
            exist.
        """
        import json as _json

        from omnigent.entities.pagination import paginate_in_memory

        validated = _validate_path(path) if path else ""
        target = validated or "."

        # Shell-quote the generated script; target is embedded via json.dumps.
        # Per-entry try/except handles broken symlinks.
        _script = "\n".join(
            [
                "import os, json",
                f"d = {_json.dumps(target)}",
                "es = []",
                "for e in sorted(os.listdir(d)):",
                "    p = os.path.join(d, e)",
                "    try:",
                "        st = os.stat(p)",
                "        t = 'd' if os.path.isdir(p) else 'f'",
                "        es.append({'n': e, 's': st.st_size if t == 'f' else None,",
                "            'm': int(st.st_mtime), 't': t})",
                "    except OSError:",
                "        try:",
                "            ls = os.lstat(p)",
                "            es.append({'n': e, 's': None, 'm': int(ls.st_mtime), 't': 'f'})",
                "        except OSError:",
                "            pass",
                "print(json.dumps(es))",
            ]
        )
        result = await _run_os_env_async(
            self._os_env.shell,
            f"python3 -c {_shell_quote(_script)}",
        )
        if "error" in result:
            raise FilesystemPathNotFound(f"Directory {path!r} not found or not accessible")

        entries: list[FilesystemEntry] = []
        try:
            raw = _json.loads(result.get("stdout", "[]"))
        except _json.JSONDecodeError:
            raw = []
        for item in raw:
            name = item["n"]
            rel = os.path.join(validated, name) if validated else name
            entry_type = "directory" if item["t"] == "d" else "file"
            entries.append(
                FilesystemEntry(
                    id=rel,
                    name=name,
                    path=rel,
                    type=entry_type,
                    bytes=item["s"] if entry_type == "file" else None,
                    modified_at=item["m"],
                )
            )

        return paginate_in_memory(
            entries,
            id_fn=lambda e: e.id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

    async def search_files(
        self,
        query: str,
        *,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        limit: int = 500,
    ) -> list[FilesystemEntry]:
        """Search for files recursively by name/path substring and glob filters.

        Walks the full directory tree via ``os.walk()`` inside the sandbox and
        returns files that satisfy all of the supplied filters:

        - ``exclude`` (highest priority): the file is dropped if its path
          matches any exclude glob. Excluded subtrees are pruned from the
          walk where possible, so a pattern like ``"**/node_modules"`` avoids
          descending into those directories.
        - ``include``: when non-empty, the file is kept only if its path
          matches at least one include glob.
        - ``query``: when non-empty, the file's name or relative path must
          contain ``query`` (case-insensitive substring match).

        Glob patterns use the VSCode/Cursor subset documented on
        :func:`_glob_to_regex` and are matched case-insensitively. Only files
        (not directories) are returned, capped at ``limit`` entries.

        A non-empty ``query`` is required: a whitespace-only query would match
        every file, so the method returns an empty list instead of walking the
        whole tree. ``include`` / ``exclude`` only narrow the query's results.

        :param query: Case-insensitive substring to match against filename
            and relative path, e.g. ``"test.md"``. Whitespace-only strings
            return an empty list immediately without walking the tree.
        :param include: Pre-split, non-blank glob patterns scoping which files
            are returned, e.g. ``["*.ts", "src/**"]``. ``None`` or empty means
            no include filter. Parse query strings with :func:`split_glob_list`
            before calling — this method does not re-split or strip.
        :param exclude: Pre-split, non-blank glob patterns for files to drop,
            e.g. ``["**/node_modules", "*.test.ts"]``. ``None`` or empty means
            no exclude filter.
        :param limit: Maximum number of results to return, e.g. ``500``.
        :returns: Flat list of matching filesystem entries, sorted by path (ascending).
        :raises FilesystemPathNotFound: If the root directory is not accessible.
        """
        import json as _json

        q = query.strip().lower()
        if not q:
            # A query is required; a whitespace-only query would match every
            # file, so return nothing instead of walking the whole tree.
            return []

        include_regexes = [_glob_to_regex(p) for p in (include or [])]
        exclude_regexes = [_glob_to_regex(p) for p in (exclude or [])]

        # All caller-derived values (q and the pre-translated regexes) are
        # embedded via json.dumps so they become valid Python literals and
        # cannot inject code; the whole script is shell-quoted below.
        _script = "\n".join(
            [
                "import os, json, re",
                f"q = {_json.dumps(q)}",
                f"limit = {limit}",
                f"inc = [re.compile(p, re.IGNORECASE) for p in {_json.dumps(include_regexes)}]",
                f"exc = [re.compile(p, re.IGNORECASE) for p in {_json.dumps(exclude_regexes)}]",
                "results = []",
                "for dirpath, dirnames, filenames in os.walk('.'):",
                "    # Prune excluded subtrees (e.g. node_modules) from the walk.",
                "    kept = []",
                "    for d in sorted(dirnames):",
                "        dp = os.path.normpath(os.path.join(dirpath, d))",
                "        if any(r.match(dp) for r in exc):",
                "            continue",
                "        kept.append(d)",
                "    dirnames[:] = kept",
                "    for fname in sorted(filenames):",
                "        p = os.path.normpath(os.path.join(dirpath, fname))",
                "        if exc and any(r.match(p) for r in exc):",
                "            continue",
                "        if inc and not any(r.match(p) for r in inc):",
                "            continue",
                "        if q not in fname.lower() and q not in p.lower():",
                "            continue",
                "        try:",
                "            st = os.stat(p)",
                "            results.append({'n': fname, 'p': p, 's': st.st_size,",
                "                'm': int(st.st_mtime)})",
                "        except OSError:",
                "            results.append({'n': fname, 'p': p, 's': None, 'm': None})",
                "        if len(results) >= limit:",
                "            break",
                "    if len(results) >= limit:",
                "        break",
                "print(json.dumps(results))",
            ]
        )
        result = await _run_os_env_async(
            self._os_env.shell,
            f"python3 -c {_shell_quote(_script)}",
        )
        if "error" in result:
            raise FilesystemPathNotFound(f"Root directory not accessible: {result['error']}")

        entries: list[FilesystemEntry] = []
        stdout = result.get("stdout", "")
        try:
            raw = _json.loads(stdout)
        except _json.JSONDecodeError as exc:
            raise RuntimeError(
                f"search_files: unexpected output from sandbox script: {stdout!r}"
            ) from exc
        for item in raw:
            entries.append(
                FilesystemEntry(
                    id=item["p"],
                    name=item["n"],
                    path=item["p"],
                    type="file",
                    bytes=item["s"],
                    modified_at=item["m"],
                )
            )
        entries.sort(key=lambda e: e.path)
        return entries

    async def read(
        self,
        path: str,
        *,
        max_bytes: int | None = None,
        limit: int | None = _DEFAULT_READ_LIMIT,
    ) -> FileContent:
        """Read file content via the sandboxed helper.

        Uses ``os_env.read()`` so the sandbox enforces read access.

        :param path: Relative file path.
        :param max_bytes: Maximum bytes to read. Defaults to
            ``_MAX_READ_BYTES`` (10 MiB).
        :param limit: Maximum number of lines to return.  Defaults to
            ``_DEFAULT_READ_LIMIT`` (2 000 lines) — appropriate for agent
            tool calls.  Pass ``None`` for no line cap (e.g. the diff
            endpoint needs the full file to render a correct before/after
            view).
        :returns: The file content.
        :raises FilesystemPathNotFound: If the file does not exist.
        :raises FileTooLarge: If the file exceeds the size limit.
        """
        validated = _validate_path(path) if path else ""
        if not validated:
            raise InvalidPath("Cannot read the environment root")

        result = await _run_os_env_async(
            self._os_env.read,
            validated,
            limit=limit,
        )
        if "error" in result:
            raise FilesystemPathNotFound(f"Path {path!r} not found")

        content_str = result.get("content", "")
        data = content_str.encode("utf-8")

        byte_cap = max_bytes or _MAX_READ_BYTES
        byte_truncated = len(data) > byte_cap
        if byte_truncated:
            data = data[:byte_cap]

        # Also flag truncation when the line cap was hit.
        returned_lines = result.get("returned_lines")
        total_lines = result.get("total_lines")
        line_truncated = (
            returned_lines is not None and total_lines is not None and returned_lines < total_lines
        )

        return FileContent(
            path=path,
            data=data,
            bytes=len(data),
            encoding="utf-8",
            truncated=byte_truncated or line_truncated,
        )

    async def write(
        self,
        path: str,
        content: bytes,
        *,
        create_parents: bool = True,  # noqa: ARG002
    ) -> WriteFileResult:
        """Write/replace a file via the sandboxed helper.

        Uses ``os_env.write()`` so the sandbox enforces write access.
        The ``create_parents`` param is accepted for API compat but
        the OSEnvironment.write() always creates parents.

        :param path: Relative file path.
        :param content: Bytes to write.
        :param create_parents: Accepted for API compat; OSEnvironment
            always creates parents.
        :returns: Write result with change tracking.
        """
        validated = _validate_path(path)
        content_str = content.decode("utf-8")

        result = await _run_os_env_async(
            self._os_env.write,
            validated,
            content_str,
        )
        if "error" in result:
            raise FilesystemPathNotFound(result.get("error", f"Write failed for {path!r}"))

        created = result.get("created", False)
        bytes_written = result.get("bytes_written", len(content))

        full = self._resolve(path)
        entry = _entry_from_stat(
            self._root,
            full,
            str(full.relative_to(self._root)),
        )
        return WriteFileResult(
            operation="write",
            path=path,
            created=created,
            bytes_written=bytes_written,
            entry=entry,
        )

    async def stat(self, path: str) -> FilesystemEntry:
        """Return metadata for a single path via the sandboxed helper.

        :param path: Relative path within the environment.
        :returns: The filesystem entry.
        :raises FilesystemPathNotFound: If the path does not exist.
        """
        import json as _json

        validated = _validate_path(path) if path else ""
        target = validated or "."
        # Embed the path as a Python literal via json.dumps and shell-quote
        # the entire script (matching list_dir/search_files). This keeps the
        # caller-controlled path out of any shell-interpreted context: it never
        # reaches the shell as bare text, so $(...), backticks, and $var in the
        # path cannot be expanded.
        _script = "\n".join(
            [
                "import os, json, stat as S",
                f"p = {_json.dumps(target)}",
                "s = os.stat(p)",
                "print(json.dumps({'s': s.st_size, 'm': int(s.st_mtime),",
                "    'd': S.S_ISDIR(s.st_mode), 'l': S.S_ISLNK(s.st_mode)}))",
            ]
        )
        result = await _run_os_env_async(
            self._os_env.shell,
            f"python3 -c {_shell_quote(_script)}",
        )
        if "error" in result or result.get("exit_code", 1) != 0:
            raise FilesystemPathNotFound(f"Path {path!r} not found")
        try:
            info = _json.loads(result.get("stdout", "{}"))
        except _json.JSONDecodeError as exc:
            raise FilesystemPathNotFound(f"Path {path!r} not found") from exc
        name = os.path.basename(validated) if validated else ""
        entry_type = "file"
        if info.get("d"):
            entry_type = "directory"
        elif info.get("l"):
            entry_type = "symlink"
        return FilesystemEntry(
            id=validated or ".",
            name=name or ".",
            path=validated,
            type=entry_type,
            bytes=info["s"] if entry_type == "file" else None,
            modified_at=info["m"],
        )

    async def edit_text(
        self,
        path: str,
        edit: TextEditRequest,
    ) -> EditFileResult:
        """Edit a file via the sandboxed helper.

        Uses ``os_env.edit()`` so the sandbox enforces write access.
        Supports single-pair and batch modes.

        :param path: Relative file path.
        :param edit: The edit request.
        :returns: Edit result with change tracking.
        :raises FilesystemPathNotFound: If the file does not exist.
        :raises InvalidPath: If neither single nor batch mode params
            are provided.
        """
        from omnigent.entities.environment_filesystem import (
            TextReplacement,
        )

        validated = _validate_path(path)

        replacements: list[TextReplacement] = []
        if edit.edits is not None:
            replacements = edit.edits
        elif edit.old_text is not None and edit.new_text is not None:
            replacements = [
                TextReplacement(
                    old_text=edit.old_text,
                    new_text=edit.new_text,
                ),
            ]
        else:
            raise InvalidPath("old_text/new_text or edits list is required")

        if len(replacements) == 1:
            result = await _run_os_env_async(
                self._os_env.edit,
                validated,
                old_text=replacements[0].old_text,
                new_text=replacements[0].new_text,
            )
        else:
            edits_list = [{"oldText": r.old_text, "newText": r.new_text} for r in replacements]
            result = await _run_os_env_async(
                self._os_env.edit,
                validated,
                edits=edits_list,
            )

        if "error" in result:
            raise FilesystemPathNotFound(result.get("error", f"Edit failed for {path!r}"))

        total_count = result.get("replacements", len(replacements))
        bytes_written = result.get("bytes_written", 0)

        full = self._resolve(path)
        entry = _entry_from_stat(
            self._root,
            full,
            str(full.relative_to(self._root)),
        )
        return EditFileResult(
            operation="edit",
            path=path,
            replacements=total_count,
            bytes_before=None,
            bytes_after=bytes_written if bytes_written else None,
            entry=entry,
        )

    async def _stat_via_shell(self, validated: str) -> tuple[int, bool]:
        """Stat a path via the sandboxed helper.

        :param validated: Validated relative path.
        :returns: Tuple of (size_bytes, is_directory).
        :raises FilesystemPathNotFound: If the path does not exist.
        """
        import json as _json

        # Embed the path as a Python literal via json.dumps and shell-quote
        # the entire script so the caller-controlled path is never interpreted
        # by the shell; see stat() for the full rationale.
        _script = "\n".join(
            [
                "import os, json, stat as S",
                f"p = {_json.dumps(validated)}",
                "s = os.stat(p)",
                "print(json.dumps({'s': s.st_size, 'd': S.S_ISDIR(s.st_mode)}))",
            ]
        )
        result = await _run_os_env_async(
            self._os_env.shell,
            f"python3 -c {_shell_quote(_script)}",
        )
        if "error" in result or result.get("exit_code", 1) != 0:
            raise FilesystemPathNotFound(f"Path {validated!r} not found")
        try:
            info = _json.loads(result.get("stdout", "{}"))
        except _json.JSONDecodeError as exc:
            raise FilesystemPathNotFound(
                f"Path {validated!r} not found",
            ) from exc
        return info.get("s", 0), info.get("d", False)

    async def _check_dir_empty(self, validated: str) -> bool:
        """Check if a directory is empty via the sandboxed helper.

        :param validated: Validated relative path.
        :returns: ``True`` if the directory has children.
        """
        import json as _json

        # Embed the path as a Python literal via json.dumps and shell-quote
        # the entire script so the caller-controlled path is never interpreted
        # by the shell; see stat() for the full rationale.
        _script = "\n".join(
            [
                "import os",
                f"p = {_json.dumps(validated)}",
                "print(len(os.listdir(p)))",
            ]
        )
        check = await _run_os_env_async(
            self._os_env.shell,
            f"python3 -c {_shell_quote(_script)}",
        )
        count = int(check.get("stdout", "0").strip() or "0")
        return count > 0

    async def delete(
        self,
        path: str,
        *,
        recursive: bool = False,
    ) -> DeleteFilesystemResult:
        """Delete a file or directory via the sandboxed helper.

        :param path: Relative path. Root deletion is rejected.
        :param recursive: Allow recursive directory deletion.
        :returns: Delete result.
        :raises InvalidPath: If attempting to delete the root.
        :raises FilesystemPathNotFound: If the path does not exist.
        :raises DirectoryNotEmpty: If non-empty without recursive.
        """
        validated = _validate_path(path)
        if not validated:
            raise InvalidPath("Cannot delete the environment root")

        size, is_dir = await self._stat_via_shell(validated)

        if is_dir and not recursive and await self._check_dir_empty(validated):
            raise DirectoryNotEmpty(
                f"Directory {path!r} is not empty; use recursive=true to delete"
            )

        cmd = f"rm -rf {_shell_quote(validated)}" if is_dir else f"rm -f {_shell_quote(validated)}"
        result = await _run_os_env_async(self._os_env.shell, cmd)
        if "error" in result and result.get("exit_code", 0) != 0:
            raise FilesystemPathNotFound(
                result.get("error", f"Delete failed for {path!r}"),
            )

        return DeleteFilesystemResult(
            path=path,
            deleted=True,
            type="directory" if is_dir else "file",
            bytes_deleted=size if not is_dir else None,
        )
