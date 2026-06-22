"""Telemetry integration tests.

The DefaultExecutor tests that formerly lived here were removed when
DefaultExecutor was deleted — production dispatch routes through
harness subprocesses over HTTP; the in-process executor is no longer
part of the codebase.

Per-harness telemetry is exercised by the harness-specific test
suites under ``tests/inner/``.
"""
