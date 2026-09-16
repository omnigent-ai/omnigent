from __future__ import annotations

import base64
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.errors import OmnigentError
from omnigent.inner.credential_proxy import (
    RefreshingSecretProvider,
    prepare_credential_proxy_runtime,
)
from omnigent.inner.datamodel import CredentialSourceSpec
from omnigent.inner.egress.proxy import EgressProxy
from omnigent.spec.parser import _parse_credential_proxy


@pytest.mark.parametrize("source_kind", ["file", "command"])
def test_running_proxy_picks_up_rotated_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("first-token")
    source: dict[str, object] = {"refresh_interval_seconds": 60}
    if source_kind == "file":
        source["file"] = str(token_file)
    else:
        script = "import pathlib, sys; print(pathlib.Path(sys.argv[1]).read_text())"
        source["command"] = shlex.join([sys.executable, "-c", script, str(token_file)])
    spec = _parse_credential_proxy([{"type": "gh_basic", "source": source}])
    clock = Mock(return_value=0.0)
    monkeypatch.setattr(
        "omnigent.inner.credential_proxy.RefreshingSecretProvider",
        partial(RefreshingSecretProvider, clock=clock),
    )
    runtime = prepare_credential_proxy_runtime(spec, parent_env={})
    placeholders = dict(runtime.helper_env_updates)
    token_file.write_text("second-token")

    def auth_values() -> set[str]:
        return {EgressProxy._format_real_auth(rule) for rule in runtime.rewrites}

    first_basic = base64.b64encode(b"x-access-token:first-token").decode()
    assert auth_values() == {"token first-token", f"Basic {first_basic}"}
    clock.return_value = 60.0
    second_basic = base64.b64encode(b"x-access-token:second-token").decode()
    assert auth_values() == {"token second-token", f"Basic {second_basic}"}
    assert runtime.helper_env_updates == placeholders
    assert all(value.startswith("oa_cred_") for value in placeholders.values())
    assert all(rule.real_secret is None for rule in runtime.rewrites)


def test_failed_refresh_retries_without_reusing_cached_secret(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("initial")
    clock = Mock(return_value=0.0)
    provider = RefreshingSecretProvider(
        CredentialSourceSpec(kind="file", path=str(token_file), refresh_interval_seconds=60),
        parent_env={},
        clock=clock,
    )
    assert provider.resolve() == "initial"
    clock.return_value = 60.0
    token_file.unlink()
    with pytest.raises(ValueError, match="does not exist"):
        provider.resolve()
    token_file.write_text("")
    with pytest.raises(ValueError, match="empty"):
        provider.resolve()
    token_file.write_text("recovered")
    assert provider.resolve() == "recovered"


def test_concurrent_requests_share_a_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve = Mock(side_effect=["initial", "replacement"])
    monkeypatch.setattr("omnigent.inner.credential_proxy._resolve_secret", resolve)
    clock = Mock(return_value=0.0)
    provider = RefreshingSecretProvider(
        CredentialSourceSpec(kind="command", command="token-broker", refresh_interval_seconds=60),
        parent_env={},
        clock=clock,
    )
    assert provider.resolve() == "initial"
    clock.return_value = 60.0
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: provider.resolve(), range(16)))
    assert results == ["replacement"] * 16
    assert resolve.call_count == 2


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan"), True, "60"])
def test_parser_rejects_invalid_refresh_interval(interval: object) -> None:
    with pytest.raises(OmnigentError, match="refresh_interval_seconds"):
        _parse_credential_proxy(
            [
                {
                    "type": "gh_basic",
                    "source": {"command": "token-broker", "refresh_interval_seconds": interval},
                }
            ]
        )


def test_environment_sources_cannot_refresh() -> None:
    with pytest.raises(OmnigentError, match="requires a file or command"):
        _parse_credential_proxy(
            [{"type": "gh_basic", "source": {"env": "TOKEN", "refresh_interval_seconds": 60}}]
        )


def test_refresh_remains_opt_in(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("initial")
    spec = _parse_credential_proxy([{"type": "gh_basic", "source": {"file": str(token_file)}}])
    runtime = prepare_credential_proxy_runtime(spec, parent_env={})
    token_file.write_text("replacement")
    assert {rule.resolve_secret() for rule in runtime.rewrites} == {"initial"}
