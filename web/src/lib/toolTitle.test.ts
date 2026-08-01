import { describe, expect, it } from "vitest";
import { formatToolRunLabel, formatToolTitle } from "./toolTitle";

describe("formatToolTitle", () => {
  it("strips sys_os_shell down to the command string with no bold verb", () => {
    expect(formatToolTitle("sys_os_shell", { command: "ls -la" })).toEqual({
      verb: null,
      body: "ls -la",
    });
  });

  it("formats sys_os_read / write / edit with the verb bold and path as body", () => {
    expect(formatToolTitle("sys_os_read", { path: "/tmp/foo.py" })).toEqual({
      verb: "Read",
      body: "/tmp/foo.py",
    });
    expect(formatToolTitle("sys_os_write", { path: "out.txt", content: "x" })).toEqual({
      verb: "Write",
      body: "out.txt",
    });
    expect(formatToolTitle("sys_os_edit", { path: "a.py", oldText: "x", newText: "y" })).toEqual({
      verb: "Edit",
      body: "a.py",
    });
  });

  it("formats sys_session_send/close with tool-session identity", () => {
    const args = { tool: "researcher", session: "auth", args: "{}" };
    expect(formatToolTitle("sys_session_send", args)).toEqual({
      verb: "Start child session:",
      body: "'researcher - auth'",
    });
    expect(formatToolTitle("sys_session_close", args)).toEqual({
      verb: "Close child session:",
      body: "'researcher - auth'",
    });
  });

  it("formats sys_session_get_history with the conversation_id as body", () => {
    expect(formatToolTitle("sys_session_get_history", { conversation_id: "conv_abc123" })).toEqual({
      verb: "Get session history:",
      body: "conv_abc123",
    });
    expect(formatToolTitle("sys_session_get_history", {})).toEqual({
      verb: "Get session history",
      body: "",
    });
  });

  it("formats argument-less tools as verb-only (empty body)", () => {
    expect(formatToolTitle("sys_session_list", {})).toEqual({
      verb: "List child sessions",
      body: "",
    });
    expect(formatToolTitle("sys_read_inbox", {})).toEqual({
      verb: "Read inbox",
      body: "",
    });
    expect(formatToolTitle("sys_terminal_list", {})).toEqual({
      verb: "List terminals",
      body: "",
    });
    expect(formatToolTitle("list_tasks", {})).toEqual({
      verb: "List tasks",
      body: "",
    });
    expect(formatToolTitle("list_tasks", { filter: "completed" })).toEqual({
      verb: "List tasks",
      body: "",
    });
  });

  it("formats sys_timer_set with seconds and repeat flag", () => {
    expect(formatToolTitle("sys_timer_set", { seconds: 30 })).toEqual({
      verb: "Set timer:",
      body: "30s",
    });
    expect(formatToolTitle("sys_timer_set", { seconds: 5, repeat: true })).toEqual({
      verb: "Set timer:",
      body: "5s (repeat)",
    });
  });

  it("formats terminal tools with `<terminal>:<session>` identity", () => {
    const args = { terminal: "tmux", session: "dev" };
    expect(formatToolTitle("sys_terminal_launch", args)).toEqual({
      verb: "Launch terminal",
      body: "'tmux:dev'",
    });
    expect(formatToolTitle("sys_terminal_read", args)).toEqual({
      verb: "Read terminal",
      body: "'tmux:dev'",
    });
    expect(formatToolTitle("sys_terminal_send", { ...args, text: "ls" })).toEqual({
      verb: "Send to 'tmux:dev':",
      body: "ls",
    });
    expect(formatToolTitle("sys_terminal_send", { ...args, keys: "Enter" })).toEqual({
      verb: "Send to 'tmux:dev':",
      body: "Enter",
    });
  });

  it("formats web_search / web_fetch with quoted query", () => {
    expect(formatToolTitle("web_search", { query: "claude 4.7" })).toEqual({
      verb: "Web search:",
      body: '"claude 4.7"',
    });
    expect(formatToolTitle("web_fetch", { query: "anthropic" })).toEqual({
      verb: "Web fetch:",
      body: '"anthropic"',
    });
    expect(formatToolTitle("web_fetch", { url: "https://example.com" })).toEqual({
      verb: "Web fetch:",
      body: "https://example.com",
    });
  });

  it("falls back to `name(argsSummary)` (no bold verb) for unknown tools", () => {
    expect(formatToolTitle("my_custom_tool", { x: 1 }, '{"x":1}')).toEqual({
      verb: null,
      body: 'my_custom_tool({"x":1})',
    });
  });

  it("falls back to bare name when argsSummary is missing or empty", () => {
    expect(formatToolTitle("my_custom_tool", {})).toEqual({
      verb: null,
      body: "my_custom_tool",
    });
    expect(formatToolTitle("my_custom_tool", {}, "")).toEqual({
      verb: null,
      body: "my_custom_tool",
    });
  });

  it("formats Claude Code native tools like the CLI's step lines", () => {
    // Bash prefers the model-written description (what the TUI prints)
    // over the raw command; without one, the command shows.
    expect(
      formatToolTitle("Bash", { command: "ls docs", description: "List docs directory" }),
    ).toEqual({ verb: null, body: "List docs directory" });
    expect(formatToolTitle("Bash", { command: "ls docs" })).toEqual({
      verb: null,
      body: "ls docs",
    });
    expect(formatToolTitle("Read", { file_path: "/repo/README.md" })).toEqual({
      verb: "Read",
      body: "/repo/README.md",
    });
    expect(formatToolTitle("Edit", { file_path: "a.py" })).toEqual({ verb: "Edit", body: "a.py" });
    expect(formatToolTitle("Grep", { pattern: "TODO" })).toEqual({
      verb: "Search:",
      body: "TODO",
    });
    expect(formatToolTitle("TodoWrite", { todos: [] })).toEqual({
      verb: "Update todos",
      body: "",
    });
    expect(formatToolTitle("Task", { description: "Explore auth module" })).toEqual({
      verb: "Sub-agent:",
      body: "Explore auth module",
    });
  });

  it("formats codex native tools like its TUI", () => {
    expect(formatToolTitle("shell", { command: "/bin/bash -lc 'cat notes.txt'" })).toEqual({
      verb: null,
      body: "cat notes.txt",
    });
    expect(
      formatToolTitle("apply_patch", {
        changes: [{ path: "/repo/notes.txt", kind: { type: "update" } }],
      }),
    ).toEqual({ verb: "Edit", body: "/repo/notes.txt" });
    expect(formatToolTitle("apply_patch", { changes: [{ path: "a" }, { path: "b" }] })).toEqual({
      verb: "Edit",
      body: "2 files",
    });
  });

  it("formats pi / opencode lowercase tools, honoring filePath and path args", () => {
    expect(formatToolTitle("bash", { command: "cat notes.txt" })).toEqual({
      verb: null,
      body: "cat notes.txt",
    });
    expect(formatToolTitle("read", { filePath: "/repo/notes.txt" })).toEqual({
      verb: "Read",
      body: "/repo/notes.txt",
    });
    expect(formatToolTitle("edit", { path: "notes.txt", edits: [] })).toEqual({
      verb: "Edit",
      body: "notes.txt",
    });
    expect(formatToolTitle("write", { filePath: "out.txt" })).toEqual({
      verb: "Write",
      body: "out.txt",
    });
  });

  it("falls back when a known tool's required arg is missing or wrong-typed", () => {
    expect(formatToolTitle("sys_os_shell", {}, "fallback")).toEqual({
      verb: null,
      body: "sys_os_shell(fallback)",
    });
    expect(formatToolTitle("sys_session_send", { tool: "r" }, "{tool:r}")).toEqual({
      verb: null,
      body: "sys_session_send({tool:r})",
    });
    expect(formatToolTitle("sys_os_shell", { command: 42 }, "{command:42}")).toEqual({
      verb: null,
      body: "sys_os_shell({command:42})",
    });
  });
});

describe("formatToolRunLabel", () => {
  const call = (name: string, args?: Record<string, unknown>) => ({ name, args });

  it("labels single-category folds with the CLI-style phrase, pluralized", () => {
    expect(formatToolRunLabel([call("Bash")])).toBe("Ran 1 shell command");
    expect(formatToolRunLabel([call("Bash"), call("sys_os_shell")])).toBe("Ran 2 shell commands");
    expect(formatToolRunLabel([call("Read"), call("Read")])).toBe("Read 2 files");
    expect(formatToolRunLabel([call("Edit"), call("Write"), call("MultiEdit")])).toBe(
      "Edited 3 files",
    );
    expect(formatToolRunLabel([call("Grep")])).toBe("Ran 1 search");
    expect(formatToolRunLabel([call("Grep"), call("Glob")])).toBe("Ran 2 searches");
  });

  it("recategorizes ls / cat shell commands like the native TUIs", () => {
    expect(formatToolRunLabel([call("Bash", { command: "ls docs" })])).toBe("Listed 1 directory");
    expect(
      formatToolRunLabel([
        call("Bash", { command: "ls" }),
        call("sys_os_shell", { command: "ls -la /tmp" }),
      ]),
    ).toBe("Listed 2 directories");
    expect(formatToolRunLabel([call("Bash", { command: "cat notes.txt" })])).toBe("Read 1 file");
    // `ls` embedded in a larger command is still a shell command.
    expect(formatToolRunLabel([call("Bash", { command: "echo hi && ls" })])).toBe(
      "Ran 1 shell command",
    );
  });

  it("categorizes pi / opencode lowercase tool names", () => {
    expect(
      formatToolRunLabel([
        call("bash", { command: "ls" }),
        call("bash", { command: "cat notes.txt" }),
        call("edit", { path: "notes.txt" }),
      ]),
    ).toBe("Listed 1 directory, read 1 file, edited 1 file");
    expect(formatToolRunLabel([call("read", { filePath: "a.txt" }), call("write")])).toBe(
      "Read 1 file, edited 1 file",
    );
  });

  it("unwraps codex's login-shell wrapper before categorizing", () => {
    expect(formatToolRunLabel([call("shell", { command: "/bin/bash -lc ls" })])).toBe(
      "Listed 1 directory",
    );
    expect(formatToolRunLabel([call("shell", { command: "/bin/zsh -lc 'cat notes.txt'" })])).toBe(
      "Read 1 file",
    );
    expect(formatToolRunLabel([call("shell", { command: "/bin/bash -lc 'git status'" })])).toBe(
      "Ran 1 shell command",
    );
  });

  it("joins mixed categories in fixed order, appending unrecognized tools", () => {
    expect(formatToolRunLabel([call("Read"), call("Bash"), call("Read"), call("TodoWrite")])).toBe(
      "Ran 1 shell command, read 2 files, called 1 other tool",
    );
    expect(
      formatToolRunLabel([
        call("Bash", { command: "git status" }),
        call("Bash", { command: "ls" }),
      ]),
    ).toBe("Ran 1 shell command, listed 1 directory");
  });

  it("strips the mcp__omnigent__ prefix before categorizing", () => {
    expect(formatToolRunLabel([call("mcp__omnigent__sys_os_read")])).toBe("Read 1 file");
  });

  it("falls back to a generic 'Called N tools' when no tool is recognized", () => {
    expect(formatToolRunLabel([call("turn_diff")])).toBe("Called 1 tool");
    expect(formatToolRunLabel([call("TodoWrite")])).toBe("Called 1 tool");
    expect(formatToolRunLabel([call("alpha_tool"), call("beta_tool")])).toBe("Called 2 tools");
  });
});
