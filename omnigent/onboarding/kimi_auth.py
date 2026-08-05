"""Detect a Kimi Code (``kimi``) login for the ``kimi`` harness.

The ``kimi`` CLI authenticates against Moonshot AI's backend via ``kimi login``
(an interactive OAuth flow or a Moonshot API key). It has **no** ``kimi logout``
subcommand and no first-class "am I logged in?" exit-code probe, so login state
can only be inspected from the credential file the login flow writes (verified
live against kimi CLI v0.29.1):

- ``kimi login`` writes ``~/.kimi-code/credentials/kimi-code.json`` — the file is
  present exactly when a login has completed and absent (or empty) otherwise.

Detection is file-based and subprocess-free: a non-empty credential file counts
as a completed login. Like
:func:`omnigent.onboarding.gemini_auth.gemini_login_detected`, it cannot detect
server-side revocation — its only job is to reject the "no-credential /
file-empty" case so the readiness layer can distinguish a signed-in kimi from an
installed-but-unconfigured one without spawning a subprocess.
"""

from __future__ import annotations

import os
from pathlib import Path

# Where ``kimi login`` writes its credential after a completed sign-in
# (verified against kimi CLI v0.29.1). Present and non-empty exactly when the
# user is logged in.
KIMI_CREDENTIALS_PATH: Path = (
    Path(os.path.expanduser("~")) / ".kimi-code" / "credentials" / "kimi-code.json"
)


def kimi_login_detected(creds_path: Path | None = None) -> bool:
    """Return whether ``kimi`` has a completed login on this machine.

    Subprocess-free file check: ``kimi login`` writes its credential to
    :data:`KIMI_CREDENTIALS_PATH`, so a present, non-empty file is treated as a
    completed sign-in. This cannot detect server-side revocation — it only
    rejects the "no-credential / file-empty" case. Used by the readiness layer
    to distinguish a signed-in kimi from an installed-but-unconfigured one.

    :param creds_path: A specific credential file to check; ``None`` uses
        :data:`KIMI_CREDENTIALS_PATH`.
    :returns: ``True`` when the credential file exists and is non-empty;
        ``False`` when it is missing, empty, or unreadable.
    """
    path = creds_path if creds_path is not None else KIMI_CREDENTIALS_PATH
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        # A stat/permission error on the credential path is treated as "no
        # usable credential" rather than crashing the readiness refresh.
        return False
