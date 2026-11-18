import { FileText, Globe, ListTodo, Plug, Search, Terminal, Wrench } from "lucide-react";
import { describe, expect, it } from "vitest";
import { iconForTool } from "./toolIcon";

describe("iconForTool", () => {
  it.each(["Bash", "shell", "local_shell", "bash", "sys_os_shell", "mcp__omnigent__sys_os_shell"])(
    "gives the shell tool %s the terminal icon",
    (name) => {
      expect(iconForTool(name)).toBe(Terminal);
    },
  );

  it.each([
    ["mcp__linear__get_issue", Plug],
    ["linear__get_issue", Plug],
    ["manage_todo_list", ListTodo],
    ["TodoWrite", ListTodo],
    ["Read", FileText],
    ["mcp__omnigent__sys_os_read", FileText],
    ["apply_patch", FileText],
    ["Grep", Search],
    ["WebSearch", Globe],
    ["web_read", Globe],
  ])("gives %s its category icon", (name, icon) => {
    expect(iconForTool(name)).toBe(icon);
  });

  it("falls back to the generic icon for unknown tools", () => {
    expect(iconForTool("my_custom_tool")).toBe(Wrench);
  });
});
