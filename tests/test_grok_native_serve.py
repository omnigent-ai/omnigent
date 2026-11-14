"""Unit tests for the grok-native serve spawn helpers."""

from __future__ import annotations

from omnigent.harnesses.grok_native.serve import (
    build_grok_serve_endpoint,
    grok_serve_argv,
    grok_ws_url,
)


def test_ws_url_embeds_secret() -> None:
    assert grok_ws_url("127.0.0.1", 2419, "tok") == "ws://127.0.0.1:2419/ws?server-key=tok"


def test_serve_argv_carries_bind_and_secret() -> None:
    argv = grok_serve_argv("/usr/bin/grok", port=2419, secret="tok")
    assert argv == [
        "/usr/bin/grok",
        "agent",
        "serve",
        "--bind",
        "127.0.0.1:2419",
        "--secret",
        "tok",
    ]
    argv_extra = grok_serve_argv("/usr/bin/grok", port=1, secret="s", extra_args=("--debug",))
    assert argv_extra[-1] == "--debug"


def test_build_endpoint_allocates_distinct_ports_and_secrets() -> None:
    a = build_grok_serve_endpoint()
    b = build_grok_serve_endpoint()
    assert a.port != b.port
    assert a.secret != b.secret
    assert a.ws_url.endswith(f"server-key={a.secret}")
    assert a.ws_url.startswith(f"ws://127.0.0.1:{a.port}/")
