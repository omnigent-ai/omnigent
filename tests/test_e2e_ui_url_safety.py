from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import pytest
from _pytest.config import Config

from tests.e2e_ui.url_safety import DEV_PORTS, unsafe_ui_base_url_reason


@dataclass
class _ConfigStub:
    options: dict[str, Any]

    def getoption(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)


def _pytest_configure() -> Callable[[Config], None]:
    pytest.importorskip("playwright", exc_type=ImportError)
    from tests.e2e_ui.conftest import pytest_configure

    return pytest_configure


@pytest.mark.parametrize("port", sorted(DEV_PORTS))
def test_unsafe_ui_base_url_reason_refuses_known_dev_ports(port: int) -> None:
    reason = unsafe_ui_base_url_reason(f"http://example.com:{port}")

    assert reason == f"port {port} is a known Omnigent/Vite dev port"


@pytest.mark.parametrize(
    ("ui_base_url", "expected"),
    [
        ("http://127.0.0.1:54321", "loopback address"),
        ("http://localhost:54321", "local dev host"),
        ("http://10.0.0.5:54321", "private-network address"),
        ("http://[::1]:54321", "loopback address"),
        ("not-a-url", "absolute http(s) URL"),
    ],
)
def test_unsafe_ui_base_url_reason_refuses_dev_hosts(
    ui_base_url: str,
    expected: str,
) -> None:
    reason = unsafe_ui_base_url_reason(ui_base_url)

    assert reason is not None
    assert expected in reason


def test_unsafe_ui_base_url_reason_allows_public_non_dev_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [])

    assert unsafe_ui_base_url_reason("https://example.com:443") is None


def test_unsafe_ui_base_url_reason_handles_missing_port_explicitly() -> None:
    reason = unsafe_ui_base_url_reason("http://127.0.0.1")

    assert reason == "a loopback address"


def test_pytest_configure_rejects_headed_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "1")

    with pytest.raises(pytest.UsageError, match="must run headless in CI"):
        _pytest_configure()(cast(Config, _ConfigStub({"--ui-base-url": None, "--headed": True})))


def test_pytest_configure_rejects_dev_ui_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_E2E_ALLOW_DEV_BASE_URL", raising=False)
    monkeypatch.delenv("CI", raising=False)

    with pytest.raises(pytest.UsageError, match="Refusing --ui-base-url"):
        _pytest_configure()(
            cast(
                Config,
                _ConfigStub({"--ui-base-url": "http://127.0.0.1:5173", "--headed": False}),
            )
        )
