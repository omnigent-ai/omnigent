import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ElicitationMessage } from "./ElicitationMessage";

afterEach(() => {
  cleanup();
});

function renderMessage(message: string) {
  const { container } = render(<ElicitationMessage message={message} />);
  const span = container.firstElementChild as HTMLElement;
  return { text: span.textContent, bold: span.querySelector("strong")?.textContent ?? null };
}

describe("ElicitationMessage", () => {
  it.each([
    ["Claude wants to call **Bash**", "Claude wants to call Bash", "Bash"],
    ["Claude wants to use **sys_os_shell**", "Claude wants to use sys_os_shell", "sys_os_shell"],
    [
      "Claude wants to call **mcp__linear__create_issue**",
      "Claude wants to call mcp__linear__create_issue",
      "mcp__linear__create_issue",
    ],
  ])("renders the bridges' tool name bold without the markers: %s", (message, text, tool) => {
    expect(renderMessage(message)).toEqual({ text, bold: tool });
  });

  // Policy prompts and MCP servers put raw commands and paths in the same field; a
  // balanced `**` pair inside a command must survive, as must `_` and single `*`.
  it.each([
    "Agent wants to call sys_os_shell('rm *.o *.a pkg/__init__.py'). Approve?",
    "Agent wants to call sys_os_shell('echo **x** y'). Approve?",
    'Antigravity wants to run: echo "**Done**" >> NOTES.md',
    'Antigravity wants to run: python -c "print(2**3**2)"',
    "Antigravity wants to run: ls src/**/foo/**",
    "Claude wants to call ***Bash***",
    "Claude wants to call **Bash** now",
    "find **/*.py",
    "Approve running rm -rf /tmp/cache?",
    "",
  ])("leaves any other message verbatim: %s", (message) => {
    expect(renderMessage(message)).toEqual({ text: message, bold: null });
  });
});
