import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ElicitationMessage, splitMessageRuns } from "./ElicitationMessage";

afterEach(() => {
  cleanup();
});

describe("splitMessageRuns", () => {
  it("splits the bridges' **tool name** into a bold run", () => {
    expect(splitMessageRuns("Claude wants to call **Bash**")).toEqual([
      { text: "Claude wants to call ", bold: false },
      { text: "Bash", bold: true },
    ]);
    expect(splitMessageRuns("Codex wants to run **git fetch** now")).toEqual([
      { text: "Codex wants to run ", bold: false },
      { text: "git fetch", bold: true },
      { text: " now", bold: false },
    ]);
  });

  it("keeps underscores and single asterisks literal", () => {
    // Policy prompts and MCP servers put raw commands and paths in the message;
    // a markdown pass would read `__init__` as bold and `*.o *.a` as italics.
    const message = "Agent wants to call sys_os_shell('rm *.o *.a pkg/__init__.py'). Approve?";
    expect(splitMessageRuns(message)).toEqual([{ text: message, bold: false }]);
    expect(splitMessageRuns("Claude wants to call **mcp__linear__create_issue**")).toEqual([
      { text: "Claude wants to call ", bold: false },
      { text: "mcp__linear__create_issue", bold: true },
    ]);
  });

  it("renders unbalanced or whitespace-hugging markers verbatim", () => {
    for (const message of ["find **/*.py", "a ** b", "****", "** Bash**", "**Bash **", ""]) {
      expect(splitMessageRuns(message)).toEqual(message ? [{ text: message, bold: false }] : []);
    }
  });
});

describe("ElicitationMessage", () => {
  it("renders bold runs as <strong> and drops the markers", () => {
    render(<ElicitationMessage message="Claude wants to call **Bash**" />);
    const bold = screen.getByText("Bash", { selector: "strong" });
    expect(bold.parentElement?.textContent).toBe("Claude wants to call Bash");
  });

  it("renders a plain message as-is", () => {
    render(<ElicitationMessage message="Approve running rm -rf /tmp/cache?" />);
    const text = screen.getByText("Approve running rm -rf /tmp/cache?");
    expect(text.querySelector("strong")).toBeNull();
  });
});
