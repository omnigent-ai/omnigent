default:
    @just --list

setup:
    uv sync --locked --extra all --extra dev
    uv sync --project integrations/slack --locked
    uv sync --project sdks/python-client --locked
    uv sync --project sdks/ui --locked
    npm --prefix web ci --legacy-peer-deps

check:
    just --fmt --check
    uv run --locked python scripts/normalize_uv_lock_registry.py --check uv.lock
    uv lock --check
    uv lock --project integrations/slack --check
    uv lock --project sdks/python-client --check
    uv lock --project sdks/ui --check
    uv run --locked ruff format --check .
    uv run --locked ruff check .
    uv run --project integrations/slack --locked ruff format --check integrations/slack
    uv run --project integrations/slack --locked ruff check integrations/slack
    uv run --locked python dev/lint/lint_no_skipped_tests.py
    uv run --locked python dev/lint/lint_no_global_asyncio_patch.py
    uv run --locked python scripts/sync_version_py.py --check
    uv run --locked python scripts/gen_routing_pb2.py --check
    uv run --locked basedpyright
    uv run --project integrations/slack --locked basedpyright --project integrations/slack/pyproject.toml
    uv run --project sdks/python-client --locked basedpyright --project sdks/python-client/pyproject.toml
    uv run --project sdks/ui --locked basedpyright --project sdks/ui/pyproject.toml
    npm --prefix web run format:check
    npm --prefix web run type-check

test:
    uv run --locked pytest
    ALL_PROXY= all_proxy= HTTP_PROXY= http_proxy= HTTPS_PROXY= https_proxy= uv run --project integrations/slack --locked pytest integrations/slack/tests
    NODE_OPTIONS=--no-experimental-webstorage npm --prefix web test

run:
    uv run --locked omnigent

fmt:
    uv run --locked ruff format .
    uv run --project integrations/slack --locked ruff format integrations/slack
    npm --prefix web run format

clean:
    uv run --locked python -c 'from pathlib import Path; import shutil; roots=tuple(Path(p) for p in ("omnigent", "tests", "scripts", "dev", "deploy", ".github/scripts", ".claude/skills", "integrations/slack/src", "integrations/slack/tests", "sdks/python-client/omnigent_client", "sdks/ui/omnigent_ui_sdk")); [shutil.rmtree(p, ignore_errors=True) for root in roots if root.exists() for p in root.rglob("__pycache__")]; [shutil.rmtree(Path(p), ignore_errors=True) for p in (".pytest_cache", ".ruff_cache", "integrations/slack/.pytest_cache", "integrations/slack/.ruff_cache", "sdks/python-client/.pytest_cache", "sdks/python-client/.ruff_cache", "sdks/ui/.pytest_cache", "sdks/ui/.ruff_cache")]'
