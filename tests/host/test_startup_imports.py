"""A remote host must register without loading server database dependencies."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import httpx
import pytest

from tests._helpers.host import isolated_host
from tests._helpers.live_server import isolated_local_server, isolated_process_env

# Run before any Omnigent imports in a fresh interpreter, outside pytest's module cache.
_NO_DATABASE_IMPORTS = dedent("""\
    import importlib.abc
    import sys

    def is_database_module(name):
        return any(
            name == prefix or name.startswith(prefix + ".")
            for prefix in ("sqlalchemy", "alembic", "omnigent.db")
        )

    preloaded = [name for name in sys.modules if is_database_module(name)]
    assert not preloaded, "Database modules loaded before the guard: " + repr(preloaded)

    class NoDatabaseImports(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if is_database_module(fullname):
                print(
                    "UNEXPECTED_DATABASE_IMPORT: " + fullname,
                    file=sys.__stderr__, flush=True,
                )
                raise ImportError("Host startup does not need " + fullname)

    sys.meta_path.insert(0, NoDatabaseImports())
    """)


def _run_without_database_imports(tmp_path: Path, code: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _NO_DATABASE_IMPORTS + code, *args],
        cwd=Path(__file__).resolve().parents[2],
        env=isolated_process_env(tmp_path, {}, {"PATH": os.defpath}),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    # Optional capability probes can catch ImportError; they must still fail this test.
    assert "UNEXPECTED_DATABASE_IMPORT" not in output, output


@pytest.mark.parametrize(
    "module",
    ["omnigent.cli", "omnigent.host.connect", "omnigent.runner.app", "omnigent.runtime"],
)
def test_startup_modules_do_not_import_database(tmp_path: Path, module: str) -> None:
    """Host/runner imports must stay cheap regardless of which entry point runs first."""
    _run_without_database_imports(
        tmp_path, "import importlib\nimport sys\nimportlib.import_module(sys.argv[1])\n", module
    )


def test_runner_health_without_database_imports(tmp_path: Path) -> None:
    """Wiring runner routes and serving health must not initialize server storage."""
    code = dedent("""\
        import asyncio
        import httpx

        from omnigent.runner.app import create_runner_app

        async def check_health():
            async with httpx.AsyncClient(base_url="http://localhost") as server:
                app = create_runner_app(server_client=server)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://runner"
                ) as client:
                    response = await client.get("/health")
                    assert response.status_code == 200, response.text

        asyncio.run(check_health())
        """)
    _run_without_database_imports(tmp_path, code)


@pytest.mark.parametrize("source", ["environment", "config", "generated"])
def test_host_identity_does_not_import_database(tmp_path: Path, source: str) -> None:
    """Managed IDs and saved/generated local IDs share DB-free UUID validation."""
    code = dedent("""\
        import os
        import sys
        from pathlib import Path
        from uuid import UUID, uuid4

        from omnigent.host.identity import (
            load_host_identity_if_present,
            load_or_create_host_identity,
        )

        source, filename = sys.argv[1:]
        path = Path(filename)
        host_id = uuid4().hex
        if source == "environment":
            os.environ["OMNIGENT_HOST_ID"] = "host_" + host_id
            os.environ["OMNIGENT_HOST_NAME"] = "import-test"
        elif source == "config":
            path.write_text("host:\\n  host_id: host_" + host_id + "\\n  name: import-test\\n")

        identity = load_or_create_host_identity(path)
        assert UUID(identity.host_id).hex == identity.host_id
        if source != "generated":
            assert identity.host_id == host_id
            assert identity.name == "import-test"
        assert load_or_create_host_identity(path) == identity
        assert load_host_identity_if_present(path) == identity
        """)
    _run_without_database_imports(tmp_path, code, source, str(tmp_path / "config.yaml"))


@pytest.mark.parametrize("entrypoint", ["cli", "host._daemon_entry"])
def test_host_registers_without_database_imports(tmp_path: Path, entrypoint: str) -> None:
    bootstrap = _NO_DATABASE_IMPORTS + f"from omnigent.{entrypoint} import main\nmain()\n"
    args = ["host", "--no-open", "--non-interactive"] if entrypoint == "cli" else []
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
                *args,
                "--server",
                base_url,
            ],
            cwd=Path(__file__).resolve().parents[2],
        ) as host,
    ):
        row = host.wait_online(client)
        assert row["host_id"] == host.host_id

    # Best-effort capability probes must not swallow a prohibited import either.
    assert "UNEXPECTED_DATABASE_IMPORT" not in host.log_path.read_text(errors="replace")
