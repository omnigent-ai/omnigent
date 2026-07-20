// Per-tool display title formatter for the tool-call trigger row.
//
// The transcript was full of titles like `sys_os_shell({"command":"ls
// -la"})` — accurate but visually noisy. This module rewrites the
// common built-in tool calls into short, plain-English phrases that
// match how a human would describe the action ("ls -la", "Read
// foo.py", "Start child session: 'researcher - auth'").
//
// The result is split into a neutral `verb` and a dynamic `body`. The
// body also carries a semantic kind so the trigger can add restrained
// emphasis to commands, paths, identifiers, and metrics without
// making every tool call look like a badge. Unknown tools fall back to the
// pre-existing `name(argsSummary)` shape so we never lose information for
// tools we haven't taught this module about.

export type ToolTitleBodyKind =
  | "plain"
  | "command"
  | "path"
  | "identifier"
  | "metric"
  | "query"
  | "url";

/**
 * Structured title for a tool call.
 * - `verb`: the static action phrase (e.g. "Read", "Start child
 *   session:"). Rendered in the neutral foreground by the trigger row.
 *   `null` when the body already reads as the complete action.
 * - `body`: the dynamic payload (path, command, session id). Empty
 *   string when the title is verb-only (e.g. "Read inbox").
 * - `bodyKind`: semantic presentation tier for that dynamic payload.
 */
export interface ToolTitle {
  verb: string | null;
  body: string;
  bodyKind: ToolTitleBodyKind;
}

type ArgFormatter = (args: Record<string, unknown>) => ToolTitle | null;

const FORMATTERS: Record<string, ArgFormatter> = {
  // OS-environment tools — drop the noisy `sys_os_*` prefix; the verb
  // alone communicates the action.
  sys_os_shell: (args) => {
    const cmd = asString(args.command);
    return cmd === null ? null : title(null, cmd, "command");
  },
  sys_os_read: (args) => withPath("Read", args.path),
  sys_os_write: (args) => withPath("Write", args.path),
  sys_os_edit: (args) => withPath("Edit", args.path),

  // Sub-agent session tools — single-quote the `<tool> - <session>`
  // pair so it reads as one identity.
  sys_session_send: (args) => {
    // By-session-id mode: post to an existing child by id; otherwise
    // the named (agent, title) spawn/continue form.
    const sid = asString(args.session_id);
    if (sid !== null) return title("Send to session:", sid, "identifier");
    return sessionTitle("Start child session:", args);
  },
  sys_session_create: (args) => {
    const id = asString(args.agent_id);
    return id === null ? verbOnly("Create session") : title("Create session:", id, "identifier");
  },
  sys_session_get_history: (args) => {
    const id = asString(args.conversation_id);
    return id === null
      ? verbOnly("Get session history")
      : title("Get session history:", id, "identifier");
  },
  // Intelligent routing (normally rendered by SmartRoutingCard; this covers
  // grouped/summary surfaces that fall back to the title).
  sys_advise_models: (args) => {
    const count = Array.isArray(args.tasks) ? args.tasks.length : null;
    return count === null
      ? verbOnly("Intelligent routing")
      : title("Intelligent routing:", `${count} task${count === 1 ? "" : "s"}`, "metric");
  },

  sys_session_close: (args) => sessionTitle("Close child session:", args),
  sys_session_list: () => verbOnly("List child sessions"),
  sys_session_get_info: (args) => {
    const id = asString(args.session_id);
    return id === null
      ? verbOnly("Get session info")
      : title("Get session info:", id, "identifier");
  },

  // Agent-management tools.
  sys_agent_get: (args) => {
    const id = asString(args.session_id);
    return id === null ? verbOnly("Get agent") : title("Get agent:", id, "identifier");
  },
  sys_agent_download: (args) => {
    const id = asString(args.session_id);
    return id === null ? verbOnly("Download agent") : title("Download agent:", id, "identifier");
  },
  sys_agent_list: () => verbOnly("List agents"),

  // Async dispatch + inbox.
  sys_call_async: (args) => {
    const tool = asString(args.tool);
    return tool === null
      ? verbOnly("Dispatch async")
      : title("Dispatch async:", tool, "identifier");
  },
  sys_read_inbox: () => verbOnly("Read inbox"),
  list_tasks: () => verbOnly("List tasks"),
  sys_cancel_async: (args) => {
    const id = asString(args.handle_id);
    return id === null ? verbOnly("Cancel async") : title("Cancel async:", id, "identifier");
  },
  sys_cancel_task: (args) => {
    const id = asString(args.task_id);
    return id === null ? verbOnly("Cancel task") : title("Cancel task:", id, "identifier");
  },

  // Timers.
  sys_timer_set: (args) => {
    const seconds = asNumber(args.seconds);
    if (seconds === null) return null;
    const repeat = args.repeat === true ? " (repeat)" : "";
    return title("Set timer:", `${seconds}s${repeat}`, "metric");
  },
  sys_timer_cancel: (args) => {
    const id = asString(args.timer_id);
    return id === null ? verbOnly("Cancel timer") : title("Cancel timer:", id, "identifier");
  },

  // Terminal multiplexer.
  sys_terminal_launch: (args) => terminalTitle("Launch terminal", args),
  sys_terminal_read: (args) => terminalTitle("Read terminal", args),
  sys_terminal_close: (args) => terminalTitle("Close terminal", args),
  sys_terminal_list: () => verbOnly("List terminals"),
  sys_terminal_send: (args) => {
    const id = terminalId(args);
    if (id === null) return null;
    const payload = asString(args.text) ?? asString(args.keys);
    return payload === null
      ? title("Send to", `'${id}'`, "identifier")
      : title(`Send to '${id}':`, payload, "command");
  },

  // Web tools.
  web_search: (args) => {
    const q = asString(args.query);
    return q === null ? null : title("Web search:", `"${q}"`, "query");
  },
  web_fetch: (args) => {
    const q = asString(args.query);
    if (q !== null) return title("Web fetch:", `"${q}"`, "query");
    const url = asString(args.url);
    if (url !== null) return title("Web fetch:", url, "url");
    return null;
  },
};

/**
 * Compute the title shown in a tool-call trigger row. Tries the
 * per-tool formatter first; otherwise falls back to `name(argsSummary)`
 * (or just `name` when the summary is empty) with no verb emphasis.
 */
export function formatToolTitle(
  name: string,
  args: Record<string, unknown>,
  argsSummary?: string,
): ToolTitle {
  const formatter = FORMATTERS[name];
  if (formatter !== undefined) {
    const title = formatter(args);
    if (title !== null && (title.verb !== null || title.body.length > 0)) {
      return title;
    }
  }
  const fallback =
    argsSummary !== undefined && argsSummary.length > 0 ? `${name}(${argsSummary})` : name;
  return title(null, fallback);
}

function asString(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

function asNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function verbOnly(verb: string): ToolTitle {
  return title(verb, "");
}

function withPath(verb: string, raw: unknown): ToolTitle | null {
  const path = asString(raw);
  return path === null ? null : title(verb, path, "path");
}

function sessionTitle(verb: string, args: Record<string, unknown>): ToolTitle | null {
  const tool = asString(args.tool);
  const session = asString(args.session);
  if (tool === null || session === null) return null;
  return title(verb, `'${tool} - ${session}'`, "identifier");
}

function terminalId(args: Record<string, unknown>): string | null {
  const terminal = asString(args.terminal);
  const session = asString(args.session);
  if (terminal === null || session === null) return null;
  return `${terminal}:${session}`;
}

function terminalTitle(verb: string, args: Record<string, unknown>): ToolTitle | null {
  const id = terminalId(args);
  return id === null ? null : title(verb, `'${id}'`, "identifier");
}

function title(
  verb: string | null,
  body: string,
  bodyKind: ToolTitleBodyKind = "plain",
): ToolTitle {
  return { verb, body, bodyKind };
}
