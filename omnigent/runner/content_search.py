"""Bounded ripgrep searches executed inside the session's OS environment."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from omnigent.runner.environment_filesystem import _python_script_command

if TYPE_CHECKING:
    from omnigent.inner.os_env import OSEnvironment


# This stdlib-only script runs in the environment helper, including its sandbox.
_SEARCH_SCRIPT = r"""
import base64
import json
import subprocess
import tempfile
import threading

args = ["rg", "--json", "--no-config", "--hidden",
        "--glob=!.git", "--color=never", "--", query, "."]
args[1:1] = ["--case-sensitive" if case_sensitive else "--ignore-case"]
if not regex:
    args.insert(1, "--fixed-strings")
matches = []
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
            if len(matches) == limit:
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
            matches.append({
                "path": path[2:] if path.startswith("./") else path,
                "line": data["line_number"],
                "column": utf16_length(line[:start]) + 1,
                "text": text,
                "match_start": utf16_length(prefix + line[left:start]),
                "match_end": utf16_length(prefix + line[left:min(end, right)]),
            })
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
print(json.dumps(
    {"matches": matches, "truncated": truncated, "warning": warning}, ensure_ascii=False
))
"""


async def search_content(
    environment: OSEnvironment,
    query: str,
    *,
    case_sensitive: bool = False,
    regex: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    """Search on-disk workspace text without bypassing the environment sandbox.

    :param environment: The session environment whose cwd is searched.
    :param query: A single-line literal or ripgrep regular expression.
    :param case_sensitive: Whether letter case must match.
    :param regex: Interpret the query as a regular expression.
    :param limit: Maximum matching lines returned, from 1 to 500.
    :returns: Matches and partial-result metadata, or a user-facing error.
    """
    if not query.strip() or len(query) > 4096 or any(c in query for c in "\x00\r\n"):
        return {"error": "Enter a single-line search query of 1–4096 characters."}
    if not 1 <= limit <= 500:
        return {"error": "Search limit must be between 1 and 500."}
    parameters = (
        f"query = {query!r}\ncase_sensitive = {case_sensitive!r}\n"
        f"regex = {regex!r}\nlimit = {limit!r}\n"
    )
    # ponytail: one capped response; stream batches if large-workspace latency warrants it.
    result = await environment.shell(
        _python_script_command(parameters + _SEARCH_SCRIPT),
        timeout=20,
        max_output=4 * 1024 * 1024,
    )
    if result.get("error") or result.get("exit_code"):
        return {"error": "Content search could not run in this workspace. Try again."}
    try:
        return json.loads(result.get("stdout", ""))
    except json.JSONDecodeError:
        return {"error": "Content search could not run in this workspace. Try again."}
