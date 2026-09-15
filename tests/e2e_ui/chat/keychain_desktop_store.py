"""Store-then-load a provider secret the way ``omnigent setup`` does.

Run as a script in a subprocess so the ``PYTHON_KEYRING_BACKEND`` selector in
the caller-provided environment applies to the ``keyring`` import::

    python keychain_desktop_store.py <name> <value>

Prints ``LOADED:<value>`` on success (the round-trip proves the secret landed
in the selected OS-keyring backend). Kept free of any network client on
purpose: it is the desktop-side "setup stored the key" step only.
"""

from __future__ import annotations

import sys


def main() -> None:
    """Store ``argv[1]`` under ``argv[2]`` via the real setup storage path."""
    from omnigent.onboarding import secrets

    name, value = sys.argv[1], sys.argv[2]
    secrets.store_secret(name, value)
    print("LOADED:" + str(secrets.load_secret(name)))


if __name__ == "__main__":
    main()
