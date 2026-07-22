import { describe, expect, it } from "vitest";
import { formatToolTitle } from "./toolTitle";

describe("formatToolTitle", () => {
  it("strips sys_os_shell down to the command string with no bold verb", () => {
    expect(formatToolTitle("sys_os_shell", { command: "ls -la" })).toMatchObject({
      verb: null,
      body: "ls -la",
    });
  });

  it("formats sys_os_read / write / edit with the verb bold and path as body", () => {
    expect(formatToolTitle("sys_os_read", { path: "/tmp/foo.py" })).toMatchObject({
      verb: "Read",
      body: "/tmp/foo.py",
    });
    expect(formatToolTitle("sys_os_write", { path: "out.txt", content: "x" })).toMatchObject({
      verb: "Write",
      body: "out.txt",
    });
    expect(
      formatToolTitle("sys_os_edit", { path: "a.py", oldText: "x", newText: "y" }),
    ).toMatchObject({
      verb: "Edit",
      body: "a.py",
    });
  });

  it("formats sys_session_send/close with tool-session identity", () => {
    const args = { tool: "researcher", session: "auth", args: "{}" };
    expect(formatToolTitle("sys_session_send", args)).toMatchObject({
      verb: "Start child session:",
      body: "'researcher - auth'",
    });
    expect(formatToolTitle("sys_session_close", args)).toMatchObject({
      verb: "Close child session:",
      body: "'researcher - auth'",
    });
  });

  it("formats sys_session_get_history with the conversation_id as body", () => {
    expect(
      formatToolTitle("sys_session_get_history", { conversation_id: "conv_abc123" }),
    ).toMatchObject({
      verb: "Get session history:",
      body: "conv_abc123",
    });
    expect(formatToolTitle("sys_session_get_history", {})).toMatchObject({
      verb: "Get session history",
      body: "",
    });
  });

  it("formats argument-less tools as verb-only (empty body)", () => {
    expect(formatToolTitle("sys_session_list", {})).toMatchObject({
      verb: "List child sessions",
      body: "",
    });
    expect(formatToolTitle("sys_read_inbox", {})).toMatchObject({
      verb: "Read inbox",
      body: "",
    });
    expect(formatToolTitle("sys_terminal_list", {})).toMatchObject({
      verb: "List terminals",
      body: "",
    });
    expect(formatToolTitle("list_tasks", {})).toMatchObject({
      verb: "List tasks",
      body: "",
    });
    expect(formatToolTitle("list_tasks", { filter: "completed" })).toMatchObject({
      verb: "List tasks",
      body: "",
    });
  });

  it("formats sys_timer_set with seconds and repeat flag", () => {
    expect(formatToolTitle("sys_timer_set", { seconds: 30 })).toMatchObject({
      verb: "Set timer:",
      body: "30s",
    });
    expect(formatToolTitle("sys_timer_set", { seconds: 5, repeat: true })).toMatchObject({
      verb: "Set timer:",
      body: "5s (repeat)",
    });
  });

  it("formats terminal tools with `<terminal>:<session>` identity", () => {
    const args = { terminal: "tmux", session: "dev" };
    expect(formatToolTitle("sys_terminal_launch", args)).toMatchObject({
      verb: "Launch terminal",
      body: "'tmux:dev'",
    });
    expect(formatToolTitle("sys_terminal_read", args)).toMatchObject({
      verb: "Read terminal",
      body: "'tmux:dev'",
    });
    expect(formatToolTitle("sys_terminal_send", { ...args, text: "ls" })).toMatchObject({
      verb: "Send to 'tmux:dev':",
      body: "ls",
    });
    expect(formatToolTitle("sys_terminal_send", { ...args, keys: "Enter" })).toMatchObject({
      verb: "Send to 'tmux:dev':",
      body: "Enter",
    });
  });

  it("formats web_search / web_fetch with quoted query", () => {
    expect(formatToolTitle("web_search", { query: "claude 4.7" })).toMatchObject({
      verb: "Web search:",
      body: '"claude 4.7"',
    });
    expect(formatToolTitle("web_fetch", { query: "anthropic" })).toMatchObject({
      verb: "Web fetch:",
      body: '"anthropic"',
    });
    expect(formatToolTitle("web_fetch", { url: "https://example.com" })).toMatchObject({
      verb: "Web fetch:",
      body: "https://example.com",
    });
  });

  it("falls back to `name(argsSummary)` (no bold verb) for unknown tools", () => {
    expect(formatToolTitle("my_custom_tool", { x: 1 }, '{"x":1}')).toMatchObject({
      verb: null,
      body: 'my_custom_tool({"x":1})',
    });
  });

  it("falls back to bare name when argsSummary is missing or empty", () => {
    expect(formatToolTitle("my_custom_tool", {})).toMatchObject({
      verb: null,
      body: "my_custom_tool",
    });
    expect(formatToolTitle("my_custom_tool", {}, "")).toMatchObject({
      verb: null,
      body: "my_custom_tool",
    });
  });

  it("falls back when a known tool's required arg is missing or wrong-typed", () => {
    expect(formatToolTitle("sys_os_shell", {}, "fallback")).toMatchObject({
      verb: null,
      body: "sys_os_shell(fallback)",
    });
    expect(formatToolTitle("sys_session_send", { tool: "r" }, "{tool:r}")).toMatchObject({
      verb: null,
      body: "sys_session_send({tool:r})",
    });
    expect(formatToolTitle("sys_os_shell", { command: 42 }, "{command:42}")).toMatchObject({
      verb: null,
      body: "sys_os_shell({command:42})",
    });
  });

  it("classifies dynamic bodies for restrained semantic highlighting", () => {
    expect(formatToolTitle("sys_os_shell", { command: "git status" }).bodyKind).toBe("command");
    expect(formatToolTitle("sys_os_read", { path: "src/app.ts" }).bodyKind).toBe("path");
    expect(formatToolTitle("sys_session_get_info", { session_id: "session_42" }).bodyKind).toBe(
      "identifier",
    );
    expect(formatToolTitle("sys_timer_set", { seconds: 12 }).bodyKind).toBe("metric");
    expect(formatToolTitle("web_search", { query: "tool styling" }).bodyKind).toBe("query");
    expect(formatToolTitle("web_fetch", { url: "https://example.com" }).bodyKind).toBe("url");
    expect(formatToolTitle("unknown", {}).bodyKind).toBe("plain");
  });
});
