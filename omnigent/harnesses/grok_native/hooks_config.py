"""Per-session Omnigent policy plugin for ``grok agent serve``.

Grok loads plugins per-process via ``--plugin-dir``; a plugin under the
auto-trusted ``~/.grok/plugins/`` tree carries ``hooks/hooks.json`` without a
folder-trust prompt. The generated ``PreToolUse`` **command** hook runs a
self-contained script that consults Omnigent's policy evaluate + permission
routes (allow/deny/ask all supported on the command path) and prints grok's
hook-decision JSON — the same synchronous ask-and-await pattern the
commandcode bridge mod uses.

The generated script is stdlib-only: ``grok agent serve`` runs under the
vendor CLI's env, not Omnigent's venv.
"""

from __future__ import annotations

import json
from pathlib import Path

_PLUGIN_MANIFEST_NAME = "omnigent-grok-native"
_HOOK_FILENAME = "omnigent_policy_hook.py"

# Stdlib-only hook script. Reads the PreToolUse event JSON on stdin, POSTs it
# to Omnigent's policy-evaluate route for a verdict, and on ASK posts to the
# permission-request route and blocks until the card is answered. Prints the
# grok hook-decision envelope on stdout; failures fail open (allow) unless
# OMNIGENT_HOOK_FAIL_CLOSED=1.
_HOOK_SCRIPT = '''#!/usr/bin/env python3
"""Omnigent policy hook for grok-native sessions (generated; stdlib only)."""

import json
import os
import sys
import urllib.request


def _post(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


def _decision(decision: str, reason: str = "") -> None:
    out = {"hookSpecificOutput": {"permissionDecision": decision}}
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = reason
    print(json.dumps(out))


def main() -> int:
    event = json.loads(sys.stdin.read() or "{}")
    env = os.environ
    hook_headers = json.loads(env.get("OMNIGENT_HOOK_HEADERS") or "{}")
    fail_closed = env.get("OMNIGENT_HOOK_FAIL_CLOSED") == "1"

    def _fallback() -> None:
        _decision("deny" if fail_closed else "allow",
                  "omnigent policy hook unreachable" if fail_closed else "")

    policy_url = env.get("OMNIGENT_POLICY_EVALUATE_URL")
    if not policy_url:
        _decision("allow")
        return 0
    try:
        verdict = _post(policy_url, hook_headers, event, timeout=30.0)
    except Exception:
        _fallback()
        return 0

    action = str(verdict.get("verdict") or verdict.get("action") or "ALLOW").upper()
    reason = str(verdict.get("reason") or verdict.get("message") or "")
    if action == "ALLOW":
        _decision("allow", reason)
        return 0
    if action == "DENY":
        _decision("deny", reason)
        return 0
    if action != "ASK":
        _decision("allow")
        return 0

    # ASK: surface an Omnigent permission card and await its answer.
    permission_url = env.get("OMNIGENT_PERMISSION_URL")
    if not permission_url:
        _decision("ask", reason)
        return 0
    try:
        answer = _post(permission_url, hook_headers, event, timeout=600.0)
    except Exception:
        _fallback()
        return 0
    approved = bool(answer.get("approved") or answer.get("allow"))
    _decision("allow" if approved else "deny",
              str(answer.get("reason") or reason or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

_SESSIONSTART_FILENAME = "omnigent_sessionstart_hook.py"

# Stdlib-only SessionStart hook: fetch the authored Omnigent instructions and
# print them as additionalContext so the model receives them at startup.
_SESSIONSTART_SCRIPT = '''#!/usr/bin/env python3
"""Omnigent instruction-delivery hook for grok-native (generated; stdlib only)."""

import json
import os
import sys
import urllib.request


def main() -> int:
    env = os.environ
    url = env.get("OMNIGENT_AGENT_CONTENTS_URL")
    if not url:
        return 0
    try:
        headers = json.loads(env.get("OMNIGENT_HOOK_HEADERS") or "{}")
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return 0
    text = data.get("prompt") or data.get("instructions") or ""
    if isinstance(text, dict):
        text = text.get("text") or ""
    text = str(text).strip()
    if not text:
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "Omnigent session instructions:\n" + text,
    }}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def write_grok_policy_plugin(session_id: str) -> Path:
    """Write the per-session policy plugin; return the ``--plugin-dir`` path.

    Plugins are only auto-trusted under ``~/.grok/plugins/``, so the plugin
    lives at ``~/.grok/plugins/omnigent-grok-native-<session_id>`` rather than
    inside the bridge dir — project/plugin folders elsewhere need a trust
    prompt a headless serve cannot answer. Layout grok discovers:
    ``plugin.json`` + ``hooks/hooks.json`` + the hook script the JSON
    references.
    """
    plugin_dir = Path.home() / ".grok" / "plugins" / f"omnigent-grok-native-{session_id}"
    hooks_dir = plugin_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": _PLUGIN_MANIFEST_NAME,
                "version": "0.1.0",
                "description": "Omnigent policy bridge for grok-native sessions",
            },
            indent=2,
        )
    )
    script_path = plugin_dir / _HOOK_FILENAME
    script_path.write_text(_HOOK_SCRIPT)
    script_path.chmod(0o755)
    sessionstart_path = plugin_dir / _SESSIONSTART_FILENAME
    sessionstart_path.write_text(_SESSIONSTART_SCRIPT)
    sessionstart_path.chmod(0o755)
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"python3 {script_path}",
                                "timeout": 660,
                            }
                        ],
                    }
                ],
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"python3 {sessionstart_path}",
                                "timeout": 30,
                            }
                        ],
                    }
                ],
            },
            indent=2,
        )
    )
    return plugin_dir


__all__ = ["write_grok_policy_plugin"]
