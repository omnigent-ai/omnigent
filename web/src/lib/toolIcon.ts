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
  BrainCircuit,
  CircleHelp,
  Code2,
  Download,
  FilePenLine,
  FilePlus2,
  FileSearchCorner,
  FileText,
  Globe,
  History,
  Inbox,
  Info,
  List,
  ListTodo,
  MessageSquarePlus,
  MessagesSquare,
  type LucideIcon,
  Monitor,
  PencilLine,
  Plug,
  Search,
  Send,
  Share2,
  ShieldCheck,
  Sparkles,
  SquareTerminal,
  Timer,
} from "lucide-react";

/**
 * @param name - the tool name as it arrives in the call (e.g.
 *   "sys_os_read", "web_search", "my_custom_tool").
 * @param nativeToolType - present only for native (provider-managed)
 *   tools (e.g. "web_search_call"). Takes priority over `name`.
 */
export function iconForTool(name: string, nativeToolType?: string): LucideIcon {
  if (nativeToolType !== undefined) {
    return NATIVE_ICONS[nativeToolType] ?? Sparkles;
  }

  const exact = EXACT_ICONS[name];
  if (exact !== undefined) return exact;

  // Long-tail MCP tools vary by server, but their action words are stable.
  // Classify those words into a small semantic icon set so a newly-installed
  // Jira/GitHub/Slack tool still reads correctly without another UI patch.
  const tokens = name
    .toLowerCase()
    .split(/[_:\s.-]+/)
    .filter(Boolean);
  if (tokens.some((token) => SEARCH_WORDS.has(token))) return Search;
  if (tokens.some((token) => READ_WORDS.has(token))) return FileText;
  if (tokens.some((token) => WRITE_WORDS.has(token))) return PencilLine;
  if (tokens.some((token) => NETWORK_WORDS.has(token))) return Globe;
  if (tokens.some((token) => EXECUTE_WORDS.has(token))) return SquareTerminal;
  if (tokens.some((token) => TASK_WORDS.has(token))) return Bot;
  if (tokens.some((token) => PLAN_WORDS.has(token))) return ListTodo;

  // A sparkle is a quieter, more general “agent activity” fallback than a
  // wrench, which made unrelated MCP calls look like the same hardware tool.
  return Sparkles;
}

const EXACT_ICONS: Readonly<Record<string, LucideIcon>> = {
  Read: FileText,
  NotebookRead: FileText,
  Write: FilePlus2,
  Edit: FilePenLine,
  MultiEdit: FilePenLine,
  NotebookEdit: FilePenLine,
  Bash: SquareTerminal,
  Grep: Search,
  Glob: Search,
  ToolSearch: Search,
  WebSearch: Search,
  WebFetch: Globe,
  Task: Bot,
  Agent: Bot,
  TodoWrite: ListTodo,
  TaskCreate: ListTodo,
  TaskUpdate: ListTodo,
  TaskList: ListTodo,
  TaskGet: ListTodo,
  AskUserQuestion: CircleHelp,
  EnterPlanMode: ListTodo,
  ExitPlanMode: ListTodo,
  sys_os_read: FileText,
  sys_os_write: FilePlus2,
  sys_os_edit: FilePenLine,
  sys_os_shell: SquareTerminal,
  sys_shell: SquareTerminal,
  sys_runtime_execute: SquareTerminal,
  sys_terminal_launch: SquareTerminal,
  sys_terminal_list: List,
  sys_terminal_read: FileText,
  sys_terminal_send: Send,
  sys_terminal_close: Ban,
  sys_session_create: MessageSquarePlus,
  sys_session_send: Send,
  sys_session_list: MessagesSquare,
  sys_session_get_history: History,
  sys_session_get_info: Info,
  sys_session_share: Share2,
  sys_session_close: Ban,
  sys_agent_start: Bot,
  sys_agent_list: List,
  sys_agent_get: Bot,
  sys_agent_download: Download,
  sys_advise_models: BrainCircuit,
  sys_list_models: BrainCircuit,
  sys_add_policy: ShieldCheck,
  sys_policy_registry: ShieldCheck,
  sys_call_async: Send,
  sys_read_inbox: Inbox,
  list_tasks: ListTodo,
  sys_cancel_async: Ban,
  sys_cancel_task: Ban,
  sys_timer_set: Timer,
  sys_timer_cancel: Ban,
  web_search: Search,
  web_fetch: Globe,
};

const SEARCH_WORDS = new Set(["search", "find", "query", "list", "lookup", "grep", "glob"]);
const READ_WORDS = new Set([
  "get",
  "read",
  "view",
  "show",
  "describe",
  "info",
  "inspect",
  "status",
  "history",
]);
const WRITE_WORDS = new Set([
  "write",
  "edit",
  "create",
  "update",
  "save",
  "commit",
  "add",
  "remove",
  "delete",
  "set",
  "post",
  "send",
  "push",
  "apply",
  "patch",
  "rename",
  "move",
  "upload",
  "merge",
  "close",
]);
const NETWORK_WORDS = new Set(["fetch", "http", "url", "download", "navigate", "browser"]);
const EXECUTE_WORDS = new Set([
  "run",
  "exec",
  "execute",
  "build",
  "compile",
  "deploy",
  "start",
  "stop",
  "restart",
  "trigger",
  "invoke",
  "shell",
  "terminal",
]);
const TASK_WORDS = new Set(["task", "agent", "delegate", "spawn", "session"]);
const PLAN_WORDS = new Set(["plan", "todo"]);

const NATIVE_ICONS: Record<string, LucideIcon> = {
  web_search_call: Search,
  file_search_call: FileSearchCorner,
  code_interpreter_call: Code2,
  computer_call: Monitor,
  mcp_call: Plug,
};
