from __future__ import annotations

import socket

import pytest

from tests.helpers.ui_url_safety import DEV_PORTS, unsafe_ui_base_url_reason


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
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
        ],
    )

    assert unsafe_ui_base_url_reason("https://example.com:443") is None


def test_unsafe_ui_base_url_reason_handles_missing_port_explicitly() -> None:
    reason = unsafe_ui_base_url_reason("http://127.0.0.1")

    assert reason == "a loopback address"
