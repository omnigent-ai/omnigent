import { Bot, FileText, Globe, ListTodo, Plug, Search, Terminal, Wrench } from "lucide-react";
import { describe, expect, it } from "vitest";
import { iconForTool } from "./toolIcon";

describe("iconForTool", () => {
  it.each([
    "Bash",
    "shell",
    "local_shell",
    "bash",
    "run_command",
    "sys_os_shell",
    "mcp__omnigent__sys_os_shell",
  ])("gives the shell tool %s the terminal icon", (name) => {
    expect(iconForTool(name)).toBe(Terminal);
  });

  it.each([
    ["mcp__linear__get_issue", Plug],
    ["linear__get_issue", Plug],
    ["manage_todo_list", ListTodo],
    ["TodoWrite", ListTodo],
    ["Read", FileText],
    ["view_file", FileText],
    ["read_file", FileText],
    ["edit_file", FileText],
    ["write_file", FileText],
    ["replace_file_content", FileText],
    ["mcp__omnigent__sys_os_read", FileText],
    ["apply_patch", FileText],
    ["Grep", Search],
    ["grep_search", Search],
    ["find_by_name", Search],
    ["list_dir", Search],
    ["WebSearch", Globe],
    ["web_read", Globe],
    ["search_web", Globe],
    ["read_url_content", Globe],
    ["invoke_subagent", Bot],
  ])("gives %s its category icon", (name, icon) => {
    expect(iconForTool(name)).toBe(icon);
  });

  it("falls back to the generic icon for unknown tools", () => {
    expect(iconForTool("my_custom_tool")).toBe(Wrench);
  });
});
