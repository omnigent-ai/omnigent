#!/usr/bin/env python3
"""Verify the antigravity-native (agy) CLI launch is scoped to a per-session Gemini dir.

Run this on macOS (or Linux) before and after the #1477 / #1194 fix. It answers
four questions without a server, a runner, or a real agy launch:

1. Does the CLI launch pass ``--gemini_dir=<per-session dir>``?
2. Does the Omnigent MCP relay config land in that dir (so agy gets ``sys_*``)?
3. Is ``HOME`` left real (so a keyring/Keychain-backed OAuth token still resolves)?
4. Is the user's real ``~/.gemini`` left byte-for-byte untouched?

Your real ``~/.gemini`` is never written: the home directory is redirected to a
throwaway copy first, so this is safe to run on a signed-in machine.

Usage::

    .venv/bin/python dev/verify_agy_gemini_dir.py

Exit code 0 = every check passed (post-fix). Non-zero = at least one failed, with
the failure printed and the sandbox kept for inspection. A passing run then prints
the manual steps for the part only a real agy can prove: that an agy launched this
way is actually signed in.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

import omnigent.antigravity_native as agy_cli
import omnigent.antigravity_native_bridge as bridge


class _StubClient:
    """Answers the terminal create/patch calls without a server."""

    async def post(self, url: str, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "terminal_antigravity_main", "metadata": {}},
            request=httpx.Request("POST", url),
        )

    async def patch(self, url: str, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    async def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json={}, request=httpx.Request("GET", url))


def _snapshot(root: Path) -> dict[str, str]:
    """Map every file under *root* to its contents, for an exact-equality diff."""
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


async def _run(sandbox: Path) -> tuple[list[str], dict[str, str], Path, set[str]]:
    """Drive the real CLI launch path against a fake home; return what agy would get."""
    fake_home = sandbox / "home"
    real_gemini = fake_home / ".gemini"
    (real_gemini / "antigravity-cli").mkdir(parents=True)
    (real_gemini / "config").mkdir(parents=True)
    # Stand in for a signed-in user who also has their own agy MCP config + settings.
    (real_gemini / "oauth_creds.json").write_text('{"access_token": "real-token"}')
    (real_gemini / "google_accounts.json").write_text('{"active": "user@example.com"}')
    # An opaque placeholder, not a real model id: this only has to be a setting of
    # the user's that the launch must leave alone.
    (real_gemini / "antigravity-cli" / "settings.json").write_text('{"model": "user-picked"}')
    (real_gemini / "config" / "mcp_config.json").write_text('{"mcpServers": {"mine": {}}}')

    # Redirect HOME so the real ~/.gemini is untouched, and re-point the two module
    # constants that captured Path.home() at import time (a Path.home patch alone
    # leaves them aimed at the real home).
    Path.home = classmethod(lambda _cls: fake_home)  # type: ignore[method-assign]
    bridge.AGY_APP_DATA_DIR = real_gemini / "antigravity-cli"
    bridge._AGY_ONBOARDING_MARKER = bridge.AGY_APP_DATA_DIR / "cache" / "onboarding.json"
    bridge._BRIDGE_ROOT = sandbox / "omnigent" / "antigravity-native"

    # agy itself is never launched: stub the binary resolution and the terminal call.
    agy_cli.build_agy_launch = lambda **kwargs: (  # type: ignore[assignment]
        ["agy", *(("--model", kwargs["model"]) if kwargs.get("model") else ())],
        {},
    )
    captured: dict[str, Any] = {}

    async def _fake_terminal(
        _client: Any, _session_id: str, *, argv: list[str], env: dict[str, str], **_kw: Any
    ) -> Any:
        captured["argv"], captured["env"] = list(argv), dict(env)
        return agy_cli.LaunchedAntigravityTerminal(
            terminal_id="terminal_antigravity_main", tmux_socket=None, tmux_target=None
        )

    agy_cli._launch_antigravity_terminal = _fake_terminal  # type: ignore[assignment]

    bridge_id = "verify_agy_gemini_dir"
    bridge_dir = bridge.prepare_bridge_dir(bridge_id)
    before = _snapshot(real_gemini)

    await agy_cli._launch_and_record(
        _StubClient(),  # type: ignore[arg-type]
        session_id="conv_verify",
        bridge_id=bridge_id,
        conversation_id="agy_conv_placeholder",
        resume=False,
        antigravity_args=(),
        command="agy",
        model=None,
        permission_mode=None,
        headless=True,
        startup_progress=None,
    )

    after = _snapshot(real_gemini)
    changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
    return captured["argv"], captured["env"], bridge_dir, changed


def main() -> int:
    # Kept on disk until every check passes, so a failure can be inspected.
    sandbox = Path(tempfile.mkdtemp(prefix="agy-verify-"))
    argv, env, bridge_dir, changed = asyncio.run(_run(sandbox))

    iso_gemini = bridge.agy_gemini_dir(bridge_dir)
    relay_config = iso_gemini / "config" / "mcp_config.json"
    gemini_dir_flag = next((arg for arg in argv if arg.startswith("--gemini_dir=")), None)

    failures: list[str] = []

    print("agy argv:", " ".join(argv))
    print("agy env overrides:", sorted(env) or "(none)")
    print()

    # 1. The flag agy needs to read a per-session config at all.
    if gemini_dir_flag == f"--gemini_dir={iso_gemini}":
        print(f"PASS  --gemini_dir points at the per-session dir\n      {iso_gemini}")
    else:
        failures.append(
            f"--gemini_dir missing or wrong (got {gemini_dir_flag!r}); agy will read the "
            "user's real ~/.gemini, so it sees no Omnigent relay and no sys_* tools"
        )

    # 2. The relay config, in the dir the flag points at.
    if relay_config.is_file():
        tools = json.loads(relay_config.read_text())["mcpServers"]["omnigent"]["enabledTools"]
        print(f"PASS  relay mcp_config.json written ({len(tools)} tools enabled)")
    else:
        failures.append(f"no relay mcp_config.json at {relay_config}")

    # 3. HOME must stay real or a Keychain/keyring token cannot be unlocked.
    if "HOME" not in env:
        print("PASS  HOME not overridden (keyring / macOS Keychain auth still resolves)")
    else:
        failures.append(f"HOME overridden to {env['HOME']!r}; breaks macOS Keychain auth")

    # 4. The user's own agy config must survive untouched.
    if not changed:
        print("PASS  the real ~/.gemini is byte-for-byte unchanged")
    else:
        failures.append(f"the real ~/.gemini was modified: {sorted(changed)}")

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for item in failures:
            print("  -", item)
        print(f"\nsandbox kept for inspection: {sandbox}")
        return 1

    shutil.rmtree(sandbox, ignore_errors=True)
    print("All checks passed.\n")
    print("Still worth confirming by hand on macOS (only a real agy can prove auth):")
    print("  1. agy --version                          # installed, and you are signed in")
    print("  2. agy --gemini_dir=<a fresh empty dir> --help   # the flag is accepted")
    print("  3. omnigent antigravity --server <url>    # then in the agy TUI: /mcp")
    print("     Expect '✓ omnigent' with sys_* tools, and NO login prompt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
