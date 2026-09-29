"""Tests for cold host bootstrap timing decomposition."""

from websockets.datastructures import Headers

from omnigent.host.startup_timing import HostStartupTiming, server_auth_timing_ms


def test_decomposes_local_network_and_server_phases() -> None:
    """Server work is subtracted from the final upgrade wait."""
    timing = HostStartupTiming(
        claimed_ns=1_000_000_000,
        marks_ns={
            "identity_ready": 1_010_000_000,
            "record_written": 1_015_000_000,
            "connect_import_started": 1_015_000_000,
            "connect_imported": 1_115_000_000,
            "attempt_started": 1_120_000_000,
            "headers_ready": 1_150_000_000,
            "tls_ready": 1_155_000_000,
            "upgrade_started": 1_200_000_000,
            "upgrade_accepted": 1_280_000_000,
        },
    )

    assert timing.durations_ms(server_auth_ms=25.0) == {
        "identity_config": 10.0,
        "daemon_record": 5.0,
        "host_connect_import": 100.0,
        "connect_headers": 30.0,
        "tls_context": 5.0,
        "client_bootstrap": 200.0,
        "upgrade_wait": 80.0,
        "claim_to_upgrade": 280.0,
        "server_auth_upgrade": 25.0,
        "network_handshake": 55.0,
    }


def test_caps_server_timing_at_measured_upgrade_wait() -> None:
    """A server duration cannot exceed the client-observed upgrade wait."""
    timing = HostStartupTiming(
        marks_ns={
            "upgrade_started": 1_000_000_000,
            "upgrade_accepted": 1_010_000_000,
        }
    )

    phases = timing.durations_ms(server_auth_ms=25.0)

    assert phases["upgrade_wait"] == 10.0
    assert phases["server_auth_upgrade"] == 10.0
    assert phases["network_handshake"] == 0.0


def test_parses_host_server_timing_among_other_metrics() -> None:
    """The client tolerates other standard Server-Timing entries."""
    assert (
        server_auth_timing_ms(
            {"Server-Timing": "cache;desc=hit, omnigent-host-auth;dur=12.7, app;dur=9"}
        )
        == 12.7
    )
    assert server_auth_timing_ms({"Server-Timing": "app;dur=9"}) is None


def test_parses_host_timing_from_repeated_server_timing_fields() -> None:
    """A proxy-appended timing field cannot break optional timing extraction."""
    headers = Headers()
    headers["Server-Timing"] = "proxy;dur=4.2"
    headers["Server-Timing"] = "omnigent-host-auth;dur=12.5"

    assert server_auth_timing_ms(headers) == 12.5


def test_rejects_server_timing_that_overflows_float() -> None:
    """An oversized decimal cannot inject an infinite histogram observation."""
    oversized = "1" + ("0" * 400)

    assert server_auth_timing_ms({"Server-Timing": f"omnigent-host-auth;dur={oversized}"}) is None
