from omnigent.inner.opencode_executor import _parse_listen_url


def test_parse_listen_url():
    line = "opencode server listening on http://127.0.0.1:4096"
    assert _parse_listen_url(line) == "http://127.0.0.1:4096"


def test_parse_listen_url_none():
    assert _parse_listen_url("Warning: something unrelated") is None
