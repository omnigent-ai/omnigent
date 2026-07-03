"""Unit tests for :mod:`omnigent.tools.builtins.policy`."""

from __future__ import annotations

import pytest

from omnigent.tools.builtins.policy import SysAddPolicyTool, SysPolicyRegistryTool

# ── SysAddPolicyTool ─────────────────────────────────────


class TestSysAddPolicyTool:
    """Tests for the sys_add_policy tool schema."""

    def test_name(self) -> None:
        assert SysAddPolicyTool.name() == "sys_add_policy"

    def test_description_non_empty(self) -> None:
        assert len(SysAddPolicyTool.description()) > 0

    def test_schema_shape(self) -> None:
        tool = SysAddPolicyTool()
        schema = tool.get_schema()
        assert schema["type"] == "function"
        func = schema["function"]
        assert func["name"] == "sys_add_policy"
        params = func["parameters"]
        assert params["type"] == "object"
        assert set(params["required"]) == {"name", "handler"}
        props = params["properties"]
        assert props["name"]["type"] == "string"
        assert props["handler"]["type"] == "string"
        assert props["factory_params"]["type"] == "object"

    def test_invoke_raises_not_implemented(self) -> None:
        """
        sys_add_policy is runner-dispatched; invoking it in-process
        raises NotImplementedError.
        """
        from omnigent.tools.base import ToolContext

        tool = SysAddPolicyTool()
        ctx = ToolContext(task_id="t", agent_id="a")
        with pytest.raises(NotImplementedError):
            tool.invoke("{}", ctx)


# ── SysPolicyRegistryTool ────────────────────────────────


class TestSysPolicyRegistryTool:
    """Tests for the sys_policy_registry tool schema."""

    def test_name(self) -> None:
        assert SysPolicyRegistryTool.name() == "sys_policy_registry"

    def test_description_non_empty(self) -> None:
        assert len(SysPolicyRegistryTool.description()) > 0

    def test_schema_shape(self) -> None:
        tool = SysPolicyRegistryTool()
        schema = tool.get_schema()
        assert schema["type"] == "function"
        func = schema["function"]
        assert func["name"] == "sys_policy_registry"
        params = func["parameters"]
        assert params["properties"] == {}
        assert params["required"] == []

    def test_invoke_raises_not_implemented(self) -> None:
        """Runner-dispatched; raises if called in-process."""
        from omnigent.tools.base import ToolContext

        tool = SysPolicyRegistryTool()
        ctx = ToolContext(task_id="t", agent_id="a")
        with pytest.raises(NotImplementedError):
            tool.invoke("{}", ctx)
