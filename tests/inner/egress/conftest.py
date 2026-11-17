import pytest


@pytest.fixture(autouse=True)
def clean_proxy_env(monkeypatch):
    """Keep ambient proxy env from routing egress tests through a real proxy."""
    for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.upper(), raising=False)
