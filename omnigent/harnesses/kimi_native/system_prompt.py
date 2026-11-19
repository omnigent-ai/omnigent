"""Deliver Omnigent startup instructions through a session-private Kimi ``SYSTEM.md``.

Kimi has no append-system-prompt flag. It renders ``$KIMI_CODE_HOME/SYSTEM.md``
as the default profile's prompt template, where ``${base_prompt}`` keeps the
built-in prompt. The runner writes that file into the per-session home, so the
user's workspace and global Kimi home are never modified. Kimi binds the
rendered prompt when it creates a session and restores it on resume, so edits
to the agent spec reach only a new Kimi session.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

from omnigent.harnesses.kimi_native.credentials import resolve_user_kimi_home

_SYSTEM_PROMPT_FILE = "SYSTEM.md"
_BASE_PROMPT_TEMPLATE = "${base_prompt}"
#: Variables Kimi's prompt renderer substitutes. It has no escape syntax and
#: leaves any other ``${name}`` verbatim, so only these need neutralising.
_KIMI_TEMPLATE_VARIABLES = (
    "product_name",
    "role_additional",
    "reply_style_guide",
    "notify_user_guidance",
    "todo_list_guidance",
    "os",
    "windows_notes",
    "shell",
    "cwd",
    "cwd_listing",
    "agents_md",
    "additional_dirs_info",
    "additional_dirs_section",
    "skills",
    "skills_section",
    "plugin_sections",
    "base_prompt",
)
_KIMI_VARIABLE_RE = re.compile(r"\$\{(" + "|".join(_KIMI_TEMPLATE_VARIABLES) + r")\}")


def _neutralize_kimi_variables(text: str) -> str:
    """Keep Kimi from expanding its own ``${name}`` variables inside *text*."""
    return _KIMI_VARIABLE_RE.sub("$\u200b{\\1}", text)


def write_kimi_system_prompt(session_home: Path, instructions: str) -> Path:
    """Write ``SYSTEM.md`` into *session_home*: the user's template plus *instructions*.

    The template is the user's global ``SYSTEM.md`` when one exists, else
    ``${base_prompt}``. The file is replaced atomically (mode 0600) and never
    opened for writing, so a symlink at the path is swapped, not written through.

    :param session_home: The session's private ``KIMI_CODE_HOME``.
    :param instructions: Omnigent startup instructions appended after the template.
    :returns: The written ``SYSTEM.md`` path.
    """
    session_home.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(session_home, 0o700)
    try:
        template = (resolve_user_kimi_home() / _SYSTEM_PROMPT_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        template = ""
    if not template.strip():
        template = _BASE_PROMPT_TEMPLATE
    content = template.rstrip("\n")
    if instructions.strip():
        content += "\n\n" + _neutralize_kimi_variables(instructions)
    destination = session_home / _SYSTEM_PROMPT_FILE
    fd, tmp_name = tempfile.mkstemp(dir=session_home, prefix=f".{_SYSTEM_PROMPT_FILE}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content + "\n")
        os.replace(tmp_name, destination)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return destination
