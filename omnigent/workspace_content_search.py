"""The ripgrep content search script, shared by the runner and the host.

One stdlib-only script drives ripgrep. It prints one ``{"match": …}`` JSON
line per matching line as ripgrep finds it, then a ``{"truncated",
"warning"}`` line, or an ``{"error"}`` line instead. The runner runs it inside
the session's environment and sandbox and answers in one piece; the host's
workspace API runs it directly and streams the matches.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

from omnigent.runner.environment_filesystem import split_glob_list

# The REST route's largest page, and the workspace API's.
MAX_MATCHES = 500
# The whole run is bounded at this, after ripgrep's own 15 s.
SCRIPT_TIMEOUT_S = 20
MAX_SCRIPT_OUTPUT = 4 * 1024 * 1024
COULD_NOT_RUN = "Content search could not run in this workspace. Try again."


class ContentSearchError(Exception):
    """A search the user must change: an invalid query, filter or pattern.

    :param message: User-facing explanation, e.g. ripgrep's regex error.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# This stdlib-only script runs in the environment helper, including its sandbox.
_SEARCH_SCRIPT = r"""
import base64
import json
import subprocess
import tempfile
import threading

args = ["rg", "--json", "--no-config", "--hidden", *glob_args,
        "--glob=!.git", *extra_args, "--color=never", "--", query, *search_paths]
args[1:1] = ["--case-sensitive" if case_sensitive else "--ignore-case"]
if not regex:
    args.insert(1, "--fixed-strings")
found = 0
truncated = False
warning = None

def decode(value):
    if "text" in value:
        return value["text"]
    return base64.b64decode(value["bytes"]).decode("utf-8", "replace")

def utf16_length(value):
    return len(value.encode("utf-16-le")) // 2

with tempfile.TemporaryFile() as errors:
    try:
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=errors, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError:
        print(json.dumps({"error": "Install ripgrep (rg) on the runner host to search contents."}))
        raise SystemExit
    expired = threading.Event()
    def expire():
        expired.set()
        process.kill()
    timer = threading.Timer(15, expire)
    timer.start()
    reached_end = False
    searched = False
    try:
        while True:
            # Bound even a single JSON record from a generated/minified file.
            record = process.stdout.readline(1024 * 1024)
            if not record:
                reached_end = True
                break
            if not record.endswith(b"\n"):
                warning = "Some very long matching lines were omitted."
                while record and not record.endswith(b"\n"):
                    record = process.stdout.readline(1024 * 1024)
                continue
            event = json.loads(record)
            if event["type"] == "summary":
                searched = True
            if event["type"] != "match":
                continue
            if found == limit:
                truncated = True
                break
            data = event["data"]
            path = decode(data["path"])
            content = data["lines"]
            raw = (content["text"].encode("utf-8") if "text" in content
                   else base64.b64decode(content["bytes"]))
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            first = data["submatches"][0]
            start = len(raw[:first["start"]].decode("utf-8", "replace"))
            end = len(raw[:first["end"]].decode("utf-8", "replace"))
            left = max(0, start - 80)
            right = min(len(line), left + 400)
            prefix = "…" if left else ""
            text = prefix + line[left:right] + ("…" if right < len(line) else "")
            found += 1
            print(json.dumps({"match": {
                "path": path[2:] if path.startswith("./") else path,
                "line": data["line_number"],
                "column": utf16_length(line[:start]) + 1,
                "text": text,
                "match_start": utf16_length(prefix + line[left:start]),
                "match_end": utf16_length(prefix + line[left:min(end, right)]),
            }}, ensure_ascii=False), flush=True)
    finally:
        if not reached_end and process.poll() is None:
            process.kill()
        process.wait()
        timer.cancel()
        timer.join()
        process.stdout.close()
    errors.seek(0)
    error = errors.read(4096).decode("utf-8", "replace").strip()
    if expired.is_set():
        warning = "Search stopped after 15 seconds. Narrow your query and try again."
        truncated = True
    # A summary distinguishes a search with read errors from an invalid pattern.
    if (process.returncode == 2 and searched) or (error and truncated):
        truncated = True
        warning = ((warning + " ") if warning else "") + (
            "Some files or ignore rules could not be read. "
            "Results may be incomplete or include normally ignored files."
        )
    if process.returncode not in (0, 1) and not truncated:
        print(json.dumps({
            "error": error or "ripgrep could not complete the search. Check workspace access."
        }))
        raise SystemExit
print(json.dumps({"truncated": truncated, "warning": warning}, ensure_ascii=False))
"""


def content_search_script(
    query: str,
    *,
    case_sensitive: bool = False,
    regex: bool = False,
    include: str = "",
    exclude: str = "",
    limit: int = MAX_MATCHES,
    extra_args: Sequence[str] = (),
    search_paths: Sequence[str] = (".",),
) -> str:
    """The script that searches the current directory with ripgrep.

    :param query: A single-line literal or ripgrep regular expression.
    :param case_sensitive: Whether letter case must match.
    :param regex: Interpret the query as a regular expression.
    :param include: Comma-separated ripgrep globs; explicit includes override ignore rules.
    :param exclude: Comma-separated ripgrep globs to exclude, taking precedence over includes.
    :param limit: Maximum matching lines returned, from 1 to 500.
    :param extra_args: Options the caller built, after the user's globs, so their
        globs take precedence.
    :param search_paths: Where ripgrep starts, relative to the current directory.
    :returns: Python source to run with ``python3 -I -c``.
    :raises ContentSearchError: When the query or filters are invalid.
    """
    if not query.strip() or len(query) > 4096 or any(c in query for c in "\x00\r\n"):
        raise ContentSearchError("Enter a single-line search query of 1–4096 characters.")
    if not 1 <= limit <= MAX_MATCHES:
        raise ContentSearchError("Search limit must be between 1 and 500.")
    glob_args = []
    for raw, prefix in ((include, ""), (exclude, "!")):
        if len(raw) > 4096 or any(c in raw for c in "\x00\r\n"):
            raise ContentSearchError("Enter single-line file filters of at most 4096 characters.")
        for pattern in split_glob_list(raw):
            if pattern.startswith("!"):
                raise ContentSearchError(
                    "Use the Include and Exclude fields instead of ! prefixes."
                )
            if pattern.endswith("/"):
                pattern += "**"
            glob_args.append(f"--glob={prefix}{pattern}")
    parameters = (
        f"query = {query!r}\ncase_sensitive = {case_sensitive!r}\n"
        f"regex = {regex!r}\nlimit = {limit!r}\nglob_args = {glob_args!r}\n"
        f"extra_args = {list(extra_args)!r}\nsearch_paths = {list(search_paths)!r}\n"
    )
    return parameters + _SEARCH_SCRIPT


def parse_record(line: str | bytes) -> dict[str, Any]:
    """Decode one line of the script's output.

    :raises ValueError: When it isn't a match, summary or error record.
    """
    record = json.loads(line)
    if not isinstance(record, dict) or not (
        "match" in record or "error" in record or "truncated" in record
    ):
        raise ValueError("Unexpected content search output")
    return record


def collect_records(lines: Iterable[str]) -> dict[str, Any]:
    """Gather the script's whole output into the REST route's one answer."""
    matches: list[dict[str, Any]] = []
    try:
        for line in lines:
            record = parse_record(line)
            if "match" in record:
                matches.append(record["match"])
            elif "error" in record:
                return {"error": record["error"]}
            else:
                return {
                    "matches": matches,
                    "truncated": record["truncated"],
                    "warning": record["warning"],
                }
    except ValueError:
        pass
    # No summary line: the script died, or its output was cut off.
    return {"error": COULD_NOT_RUN}
