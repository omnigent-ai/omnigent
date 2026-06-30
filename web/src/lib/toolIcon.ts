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
  Send,
  Terminal,
  Timer,
  Wrench,
} from "lucide-react";

/**
 * @param name - the tool name as it arrives in the call (e.g.
 *   "sys_os_read", "web_search", "my_custom_tool").
 * @param nativeToolType - present only for native (provider-managed)
 *   tools (e.g. "web_search_call"). Takes priority over `name`.
 */
export function iconForTool(name: string, nativeToolType?: string): LucideIcon {
  if (nativeToolType !== undefined) {
    return NATIVE_ICONS[nativeToolType] ?? Wrench;
  }
  // Order matters: more specific names (`sys_terminal_close`,
  // `sys_session_close`) must match before the broader category prefix.
  if (name === "sys_os_shell") return Terminal;
  if (/^sys_os_(read|write|edit)$/.test(name)) return FileText;
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
  if (name === "web_search" || name === "web_fetch") return Globe;
  return Wrench;
}

const NATIVE_ICONS: Record<string, LucideIcon> = {
  web_search_call: Globe,
  file_search_call: FileSearchCorner,
  code_interpreter_call: Code2,
  computer_call: Monitor,
  mcp_call: Plug,
};
