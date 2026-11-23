"""Isolated egress CA setup for process-boundary tests."""

from pathlib import Path

import pytest

from omnigent.inner.egress.ca import ensure_ca, ensure_ca_bundle


def prepare_isolated_egress_ca(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    cert, key = ensure_ca(cache_dir=cache_dir)
    bundle = ensure_ca_bundle(cert, cache_dir=cache_dir)
    monkeypatch.setattr("omnigent.inner.egress.controller.ensure_ca", lambda: (cert, key))
    monkeypatch.setattr("omnigent.inner.egress.controller.ensure_ca_bundle", lambda _: bundle)
    return cert, key, bundle
