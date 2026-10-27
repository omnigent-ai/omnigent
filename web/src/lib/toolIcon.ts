// Pick a category icon for a tool-call trigger row. Returns a lucide
// component reference (the trigger row applies sizing classes).
//
// State icons (spinner / error / cancelled) take priority over the
// category icon and are handled in `ToolCard.tsx`; this module is
// only consulted for terminal "completed" tools and for native tools
// (which arrive completed).

import {
  Ban,
  Bot,
  Code2,
  FileSearchCorner,
  FileText,
  Globe,
  Inbox,
  ListTodo,
  type LucideIcon,
  Monitor,
  Plug,
  Search,
  Send,
  Terminal,
  Timer,
  Wrench,
} from "lucide-react";
import { isShellTool, splitToolName, toolCallCategory } from "@/lib/toolTitle";

const WEB_TOOLS = new Set(["web_search", "web_fetch", "web_read", "WebSearch", "WebFetch"]);
// Plan / todo-list tools across harnesses (Claude, Codex, Pi, OpenCode).
const PLAN_TOOLS = new Set(["TodoWrite", "update_plan", "manage_todo_list", "todowrite"]);

/**
 * @param fullName - the tool name as it arrives in the call (e.g.
 *   "sys_os_read", "web_search", "my_custom_tool").
 * @param nativeToolType - present only for native (provider-managed)
 *   tools (e.g. "web_search_call"). Takes priority over `name`.
 */
export function iconForTool(fullName: string, nativeToolType?: string): LucideIcon {
  if (nativeToolType !== undefined) {
    return NATIVE_ICONS[nativeToolType] ?? Wrench;
  }
  const { server, tool: name } = splitToolName(fullName);
  if (server !== undefined) return Plug;
  // Order matters: more specific names (`sys_terminal_close`,
  // `sys_session_close`) must match before the broader category prefix.
  if (isShellTool(name)) return Terminal;
  if (WEB_TOOLS.has(name)) return Globe;
  if (PLAN_TOOLS.has(name)) return ListTodo;
  const category = toolCallCategory({ name });
  if (category === "read" || category === "edit") return FileText;
  if (category === "search" || category === "list") return Search;
  if (name === "sys_terminal_close") return Ban;
  if (name.startsWith("sys_terminal_")) return Terminal;
  if (name === "sys_session_close") return Ban;
  if (name.startsWith("sys_session_")) return Bot;
  if (name === "sys_call_async") return Send;
  if (name === "sys_read_inbox") return Inbox;
  if (name === "list_tasks") return ListTodo;
  if (name.startsWith("sys_cancel_")) return Ban;
  if (name === "sys_timer_set") return Timer;
  if (name === "sys_timer_cancel") return Ban;
  return Wrench;
}

const NATIVE_ICONS: Record<string, LucideIcon> = {
  web_search_call: Globe,
  file_search_call: FileSearchCorner,
  code_interpreter_call: Code2,
  computer_call: Monitor,
  mcp_call: Plug,
};
