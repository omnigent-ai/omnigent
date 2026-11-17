"""The CLI entry decorator stays on ``main()``, not on the identity helper."""

from __future__ import annotations

import omnigent.cli as cli


def test_main_is_wrapped_by_capture_cli_entry() -> None:
    assert hasattr(cli.main, "__wrapped__")


def test_client_identity_helper_is_not_wrapped() -> None:
    assert not hasattr(cli._client_identity_headers, "__wrapped__")
