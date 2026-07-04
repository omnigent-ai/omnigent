import pytest

from omnigent.inner.opencode_executor import (
    _ENV_GATEWAY_API_KEY,
    _ENV_GATEWAY_BASE_URL,
    _ENV_GATEWAY_PROVIDER,
    _ENV_MCP_SERVERS,
    _build_opencode_config_content,
    _resolve_mcp_servers_env,
)


def test_config_none_when_unset(monkeypatch):
    for var in (_ENV_GATEWAY_BASE_URL, _ENV_GATEWAY_API_KEY, _ENV_MCP_SERVERS):
        monkeypatch.delenv(var, raising=False)
    assert _build_opencode_config_content() is None


def test_config_gateway_default_provider(monkeypatch):
    monkeypatch.setenv(_ENV_GATEWAY_BASE_URL, "https://gw/serving-endpoints")
    monkeypatch.setenv(_ENV_GATEWAY_API_KEY, "sk-test")
    monkeypatch.delenv(_ENV_GATEWAY_PROVIDER, raising=False)
    monkeypatch.delenv(_ENV_MCP_SERVERS, raising=False)
    payload = _build_opencode_config_content()
    assert payload == {
        "provider": {
            "anthropic": {
                "options": {"baseURL": "https://gw/serving-endpoints", "apiKey": "sk-test"}
            }
        }
    }


def test_config_merges_mcp_extra(monkeypatch):
    for var in (_ENV_GATEWAY_BASE_URL, _ENV_GATEWAY_API_KEY, _ENV_MCP_SERVERS):
        monkeypatch.delenv(var, raising=False)
    payload = _build_opencode_config_content(
        mcp_extra={"omnigent": {"type": "remote", "url": "http://127.0.0.1:9/mcp"}}
    )
    assert payload == {"mcp": {"omnigent": {"type": "remote", "url": "http://127.0.0.1:9/mcp"}}}


def test_resolve_mcp_servers_env_bad_json(monkeypatch):
    monkeypatch.setenv(_ENV_MCP_SERVERS, "[]")
    with pytest.raises(ValueError):
        _resolve_mcp_servers_env()
