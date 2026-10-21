"""Log a CLI-created sandbox into an Omnigent accounts-auth server."""

from __future__ import annotations

import argparse
import getpass
import shlex

from cwsandbox import Sandbox

from omnigent.onboarding.sandboxes.cwsandbox import resolve_auth_strategy


def login(sandbox_id: str, server: str, username: str, password: str) -> None:
    """Pass account credentials through stdin to the sandbox's login command."""
    if any("\n" in value or "\r" in value for value in (username, password)):
        raise ValueError("Username and password must not contain newlines")
    sandbox = Sandbox.from_id(sandbox_id, auth=resolve_auth_strategy()).result()
    # Only the exit status is needed; keep login prompts off the output stream.
    process = sandbox.exec(
        ["bash", "-lc", f"omnigent login {shlex.quote(server)} >/dev/null 2>&1"],
        stdin=True,
        timeout_seconds=60,
    )
    process.stdin.write(f"{username}\n{password}\n".encode()).result()
    process.stdin.close().result()
    result = process.result()
    if result.returncode:
        raise SystemExit(
            f"Sandbox login failed (exit {result.returncode}). "
            "Check the server URL, accounts username/password, and network access."
        )
    print("Sandbox login saved. You can now run omnigent sandbox connect.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-id", required=True)
    parser.add_argument("--server", required=True)
    args = parser.parse_args()
    username = input("Accounts username: ").strip()
    password = getpass.getpass("Password: ")
    login(args.sandbox_id, args.server, username, password)


if __name__ == "__main__":
    main()
