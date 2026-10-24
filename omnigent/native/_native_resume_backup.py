"""Keep the first local transcript a native cold resume replaces."""

from __future__ import annotations

import logging
import os
from pathlib import Path

_logger = logging.getLogger(__name__)

RESUME_BACKUP_SUFFIX = ".omnigent-backup"


def resume_backup_path(target: Path) -> Path:
    """
    Return the backup name for a native resume transcript.

    The suffix goes after ``.jsonl`` so Omnigent's import listing and the
    native CLIs' own session pickers, which all match ``*.jsonl``, never list
    the backup as a session.

    :param target: Transcript a resume is about to replace, e.g.
        ``Path("~/.claude/projects/-repo/<sid>.jsonl")``.
    :returns: Same directory, e.g. ``<sid>.jsonl.omnigent-backup``.
    """
    return target.with_name(target.name + RESUME_BACKUP_SUFFIX)


def keep_original_resume_transcript(target: Path) -> Path | None:
    """
    Keep *target* under its backup name before a resume replaces it.

    Call right before ``os.replace(tmp, target)``, once the rebuilt
    transcript is fully written. Only the first original is kept: when a
    backup already exists (an earlier resume made it), it is left alone and
    the live file is replaced as usual. Never raises: a resume must not fail
    because its backup could not be made.

    :param target: Live transcript path, e.g.
        ``Path("~/.claude/projects/-repo/<sid>.jsonl")``.
    :returns: The backup just created, or ``None`` when there was no file to
        keep, a backup already existed, or the link failed.
    """
    backup = resume_backup_path(target)
    # A hard link, then the caller's atomic os.replace: the live name exists
    # at every point (a crash leaves the original under both names), a
    # transcript that can be GBs is never copied, and link() refuses to
    # overwrite an existing backup, so concurrent or later resumes cannot
    # replace the first original.
    try:
        os.link(target, backup)
    except (FileNotFoundError, FileExistsError):
        return None
    except OSError as exc:
        _logger.warning(
            "Could not keep the original transcript %s as %s; resuming without a backup: %s",
            target,
            backup,
            exc,
        )
        return None
    _logger.info("Kept the original transcript %s as %s before resuming", target, backup)
    return backup
