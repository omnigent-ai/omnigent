import { Terminal, Wrench } from "lucide-react";
import { describe, expect, it } from "vitest";
import { iconForTool } from "./toolIcon";

describe("iconForTool", () => {
  it.each(["Bash", "shell", "local_shell", "bash", "sys_os_shell", "mcp__omnigent__sys_os_shell"])(
    "gives the shell tool %s the terminal icon",
    (name) => {
      expect(iconForTool(name)).toBe(Terminal);
    },
  );

  it("falls back to the generic icon for unknown tools", () => {
    expect(iconForTool("my_custom_tool")).toBe(Wrench);
  });
});
