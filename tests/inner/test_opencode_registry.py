from omnigent.runtime.harnesses import _HARNESS_MODULES


def test_opencode_registered():
    assert _HARNESS_MODULES["opencode"] == "omnigent.inner.opencode_harness"
