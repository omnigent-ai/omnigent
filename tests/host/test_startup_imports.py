"""A remote host must register without loading server database dependencies."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from textwrap import dedent

import httpx

from tests._helpers.host import isolated_host
from tests._helpers.live_server import isolated_local_server


def test_host_registers_without_database_imports(tmp_path: Path) -> None:
    # A fresh interpreter catches imports hidden by pytest's already-loaded modules.
    bootstrap = dedent("""\
        import importlib.abc
        import sys

        class NoDatabaseImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] == "sqlalchemy" or (
                    fullname == "omnigent.db" or fullname.startswith("omnigent.db.")
                ):
                    print("UNEXPECTED_DATABASE_IMPORT: " + fullname, flush=True)
                    raise ImportError("Host startup does not need " + fullname)

        sys.meta_path.insert(0, NoDatabaseImports())
        from omnigent.cli import main
        main()
        """)
    # No ambient credentials or installed harnesses are needed for registration.
    base_env = {"PATH": os.defpath}
    with (
        isolated_local_server(tmp_path / "server", base_env=base_env) as base_url,
        httpx.Client(base_url=base_url, trust_env=False) as client,
        isolated_host(
            tmp_path / "host",
            base_url,
            base_env=base_env,
            command=[
                sys.executable,
                "-c",
                bootstrap,
                "host",
                "--server",
                base_url,
                "--no-open",
                "--non-interactive",
            ],
        ) as host,
    ):
        row = host.wait_online(client)
        assert row["host_id"] == host.host_id

    # Best-effort capability probes must not swallow a prohibited import either.
    assert "UNEXPECTED_DATABASE_IMPORT" not in host.log_path.read_text()
