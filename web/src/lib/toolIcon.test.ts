import {
  Ban,
  BrainCircuit,
  CircleHelp,
  Code2,
  FilePenLine,
  FilePlus2,
  FileSearchCorner,
  FileText,
  Globe,
  History,
  MessageSquarePlus,
  Monitor,
  PencilLine,
  Plug,
  Search,
  Send,
  Share2,
  Sparkles,
  SquareTerminal,
} from "lucide-react";
import { describe, expect, it } from "vitest";
import { iconForTool } from "./toolIcon";

describe("iconForTool", () => {
  it("distinguishes file reads, writes, and edits", () => {
    expect(iconForTool("sys_os_read")).toBe(FileText);
    expect(iconForTool("sys_os_write")).toBe(FilePlus2);
    expect(iconForTool("sys_os_edit")).toBe(FilePenLine);
  });

  it("recognizes provider-built-in tools before heuristic fallback", () => {
    expect(iconForTool("ToolSearch")).toBe(Search);
    expect(iconForTool("Bash")).toBe(SquareTerminal);
    expect(iconForTool("Read")).toBe(FileText);
    expect(iconForTool("Write")).toBe(FilePlus2);
    expect(iconForTool("Edit")).toBe(FilePenLine);
    expect(iconForTool("AskUserQuestion")).toBe(CircleHelp);
  });

  it("distinguishes terminal and session operations", () => {
    expect(iconForTool("sys_terminal_launch")).toBe(SquareTerminal);
    expect(iconForTool("sys_terminal_send")).toBe(Send);
    expect(iconForTool("sys_terminal_close")).toBe(Ban);
    expect(iconForTool("sys_session_create")).toBe(MessageSquarePlus);
    expect(iconForTool("sys_session_get_history")).toBe(History);
    expect(iconForTool("sys_session_share")).toBe(Share2);
  });

  it("gives model and long-tail MCP tools semantic icons", () => {
    expect(iconForTool("sys_advise_models")).toBe(BrainCircuit);
    expect(iconForTool("mcp__github__pull_requests_search")).toBe(Search);
    expect(iconForTool("mcp__jira__create_issue")).toBe(PencilLine);
    expect(iconForTool("mcp__browser__navigate")).toBe(Globe);
    expect(iconForTool("mcp__ci__run_job")).toBe(SquareTerminal);
  });

  it("maps native tools and uses a quiet sparkle fallback", () => {
    expect(iconForTool("native", "web_search_call")).toBe(Search);
    expect(iconForTool("native", "file_search_call")).toBe(FileSearchCorner);
    expect(iconForTool("native", "code_interpreter_call")).toBe(Code2);
    expect(iconForTool("native", "computer_call")).toBe(Monitor);
    expect(iconForTool("native", "mcp_call")).toBe(Plug);
    expect(iconForTool("unclassifiable_magic")).toBe(Sparkles);
  });
});
