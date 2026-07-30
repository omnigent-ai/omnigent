// Per-tool display title formatter for the tool-call trigger row.
//
// The transcript was full of titles like `sys_os_shell({"command":"ls
// -la"})` — accurate but visually noisy. This module rewrites the
// common built-in tool calls into short, plain-English phrases that
// match how a human would describe the action ("ls -la", "Read
// foo.py", "Start child session: 'researcher - auth'").
//
// The result is split into a `verb` (rendered bold by the trigger row
// so the action stands out) and a `body` (the dynamic, less-important
// payload — paths, queries, commands). Unknown tools fall back to the
// pre-existing `name(argsSummary)` shape with no bolded verb so we
// never lose information for tools we haven't taught this module about.

/**
 * Structured title for a tool call.
 * - `verb`: the static action phrase (e.g. "Read", "Start child
 *   session:"). Rendered bold/foreground by the trigger row. `null`
 *   when there's nothing to emphasize (e.g. raw shell commands, or
 *   the fallback for unknown tools).
 * - `body`: the dynamic payload (path, command, session id). Empty
 *   string when the title is verb-only (e.g. "Read inbox").
 */
export interface ToolTitle {
  verb: string | null;
  body: string;
}

type ArgFormatter = (args: Record<string, unknown>) => ToolTitle | null;

const FORMATTERS: Record<string, ArgFormatter> = {
  // OS-environment tools — drop the noisy `sys_os_*` prefix; the verb
  // alone communicates the action.
  sys_os_shell: (args) => {
    const cmd = asString(args.command);
    return cmd === null ? null : { verb: null, body: cmd };
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
    if (sid !== null) return { verb: "Send to session:", body: sid };
    return sessionTitle("Start child session:", args);
  },
  sys_session_create: (args) => {
    const id = asString(args.agent_id);
    return id === null ? verbOnly("Create session") : { verb: "Create session:", body: id };
  },
  sys_session_get_history: (args) => {
    const id = asString(args.conversation_id);
    return id === null
      ? verbOnly("Get session history")
      : { verb: "Get session history:", body: id };
  },
  // Intelligent routing (normally rendered by SmartRoutingCard; this covers
  // grouped/summary surfaces that fall back to the title).
  sys_advise_models: (args) => {
    const count = Array.isArray(args.tasks) ? args.tasks.length : null;
    return count === null
      ? verbOnly("Intelligent routing")
      : { verb: "Intelligent routing:", body: `${count} task${count === 1 ? "" : "s"}` };
  },

  sys_session_close: (args) => sessionTitle("Close child session:", args),
  sys_session_list: () => verbOnly("List child sessions"),
  sys_session_get_info: (args) => {
    const id = asString(args.session_id);
    return id === null ? verbOnly("Get session info") : { verb: "Get session info:", body: id };
  },

  // Agent-management tools.
  sys_agent_get: (args) => {
    const id = asString(args.session_id);
    return id === null ? verbOnly("Get agent") : { verb: "Get agent:", body: id };
  },
  sys_agent_download: (args) => {
    const id = asString(args.session_id);
    return id === null ? verbOnly("Download agent") : { verb: "Download agent:", body: id };
  },
  sys_agent_list: () => verbOnly("List agents"),

  // Async dispatch + inbox.
  sys_call_async: (args) => {
    const tool = asString(args.tool);
    return tool === null ? verbOnly("Dispatch async") : { verb: "Dispatch async:", body: tool };
  },
  sys_read_inbox: () => verbOnly("Read inbox"),
  list_tasks: () => verbOnly("List tasks"),
  sys_cancel_async: (args) => {
    const id = asString(args.handle_id);
    return id === null ? verbOnly("Cancel async") : { verb: "Cancel async:", body: id };
  },
  sys_cancel_task: (args) => {
    const id = asString(args.task_id);
    return id === null ? verbOnly("Cancel task") : { verb: "Cancel task:", body: id };
  },

  // Timers.
  sys_timer_set: (args) => {
    const seconds = asNumber(args.seconds);
    if (seconds === null) return null;
    const repeat = args.repeat === true ? " (repeat)" : "";
    return { verb: "Set timer:", body: `${seconds}s${repeat}` };
  },
  sys_timer_cancel: (args) => {
    const id = asString(args.timer_id);
    return id === null ? verbOnly("Cancel timer") : { verb: "Cancel timer:", body: id };
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
      ? { verb: "Send to", body: `'${id}'` }
      : { verb: `Send to '${id}':`, body: payload };
  },

  // Claude Code native harness tools — mirror the step lines its TUI
  // prints. Shell steps carry a model-written description ("List docs
  // directory") which the CLI surfaces; prefer it over the raw command.
  Bash: (args) => {
    const desc = asString(args.description);
    if (desc !== null) return { verb: null, body: desc };
    const cmd = asString(args.command);
    return cmd === null ? null : { verb: null, body: cmd };
  },
  Read: (args) => withPath("Read", args.file_path),
  Write: (args) => withPath("Write", args.file_path),
  Edit: (args) => withPath("Edit", args.file_path),
  MultiEdit: (args) => withPath("Edit", args.file_path),
  NotebookEdit: (args) => withPath("Edit", args.notebook_path),
  Grep: (args) => {
    const pattern = asString(args.pattern);
    return pattern === null ? null : { verb: "Search:", body: pattern };
  },
  Glob: (args) => {
    const pattern = asString(args.pattern);
    return pattern === null ? null : { verb: "Find files:", body: pattern };
  },
  WebSearch: (args) => {
    const q = asString(args.query);
    return q === null ? null : { verb: "Web search:", body: `"${q}"` };
  },
  WebFetch: (args) => {
    const url = asString(args.url);
    return url === null ? null : { verb: "Web fetch:", body: url };
  },
  TodoWrite: () => verbOnly("Update todos"),
  Task: (args) => {
    const desc = asString(args.description);
    return desc === null ? verbOnly("Run sub-agent") : { verb: "Sub-agent:", body: desc };
  },

  // Codex native harness tools. Codex wraps every command in a login
  // shell (`/bin/bash -lc '...'`); show the inner command like its TUI.
  shell: (args) => {
    const cmd = asString(args.command);
    return cmd === null ? null : { verb: null, body: unwrapShellCommand(cmd) };
  },
  apply_patch: (args) => {
    const changes = Array.isArray(args.changes) ? args.changes : null;
    if (changes === null || changes.length === 0) return null;
    if (changes.length === 1) {
      const only = changes[0];
      const path = only !== null && typeof only === "object" ? Reflect.get(only, "path") : null;
      const p = asString(path);
      if (p !== null) return { verb: "Edit", body: p };
    }
    return { verb: "Edit", body: `${changes.length} files` };
  },

  // Pi / OpenCode native harness tools (lowercase names). OpenCode
  // passes `filePath`, Pi passes `path`.
  bash: (args) => {
    const cmd = asString(args.command);
    return cmd === null ? null : { verb: null, body: cmd };
  },
  read: (args) => withPath("Read", args.filePath ?? args.path),
  edit: (args) => withPath("Edit", args.filePath ?? args.path),
  write: (args) => withPath("Write", args.filePath ?? args.path),

  // Web tools.
  web_search: (args) => {
    const q = asString(args.query);
    return q === null ? null : { verb: "Web search:", body: `"${q}"` };
  },
  web_fetch: (args) => {
    const q = asString(args.query);
    if (q !== null) return { verb: "Web fetch:", body: `"${q}"` };
    const url = asString(args.url);
    if (url !== null) return { verb: "Web fetch:", body: url };
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
  return { verb: null, body: fallback };
}

/**
 * Action categories for the collapsed tool-run summary line. Names cover
 * the omnigent sys_* tools plus the native harness CLIs (Claude Code's
 * Bash/Read/Edit..., Codex's shell/apply_patch). Shell calls whose
 * command is a bare `ls` re-categorize as directory listings, matching
 * the Claude Code TUI's step summaries.
 */
type RunCategory = "shell" | "list" | "read" | "edit" | "search";

/** One tool call in a folded run: its name plus (optional) arguments. */
export interface ToolRunCall {
  name: string;
  args?: Record<string, unknown>;
}

const RUN_CATEGORIES: Record<string, RunCategory> = {
  Bash: "shell",
  sys_os_shell: "shell",
  shell: "shell",
  local_shell: "shell",
  bash: "shell",
  Read: "read",
  sys_os_read: "read",
  read: "read",
  Edit: "edit",
  Write: "edit",
  MultiEdit: "edit",
  NotebookEdit: "edit",
  apply_patch: "edit",
  sys_os_edit: "edit",
  sys_os_write: "edit",
  edit: "edit",
  write: "edit",
  patch: "edit",
  Grep: "search",
  Glob: "search",
  WebSearch: "search",
  web_search: "search",
  grep: "search",
  glob: "search",
  list: "list",
};

const RUN_CATEGORY_ORDER: readonly RunCategory[] = ["shell", "list", "read", "edit", "search"];

function runPhrase(category: RunCategory, n: number): string {
  const s = n === 1 ? "" : "s";
  switch (category) {
    case "shell":
      return `ran ${n} shell command${s}`;
    case "list":
      return `listed ${n} director${n === 1 ? "y" : "ies"}`;
    case "read":
      return `read ${n} file${s}`;
    case "edit":
      return `edited ${n} file${s}`;
    case "search":
      return `ran ${n} search${n === 1 ? "" : "es"}`;
  }
}

function stripOmnigentPrefix(name: string): string {
  return name.startsWith("mcp__omnigent__") ? name.slice("mcp__omnigent__".length) : name;
}

// Login-shell wrapper Codex puts around every command, e.g.
// `/bin/bash -lc 'cat notes.txt'` or `/bin/zsh -lc ls`.
const SHELL_WRAPPER_RE = /^(?:\S+\/)?(?:bash|zsh|sh|dash|fish)\s+-l?c\s+([\s\S]+)$/;

/** Peel a login-shell wrapper (and its quotes) off a command string. */
function unwrapShellCommand(command: string): string {
  const match = SHELL_WRAPPER_RE.exec(command.trim());
  if (match === null) return command.trim();
  const inner = match[1]!.trim();
  const quote = inner[0];
  if ((quote === "'" || quote === '"') && inner.length >= 2 && inner.endsWith(quote)) {
    return inner.slice(1, -1).trim();
  }
  return inner;
}

function categorizeCall(call: ToolRunCall): RunCategory | "other" {
  const name = stripOmnigentPrefix(call.name);
  const category = RUN_CATEGORIES[name] ?? "other";
  if (category === "shell") {
    const raw = typeof call.args?.command === "string" ? call.args.command : "";
    const command = unwrapShellCommand(raw);
    if (command === "ls" || command.startsWith("ls ")) return "list";
    if (command === "cat" || command.startsWith("cat ")) return "read";
  }
  return category;
}

/**
 * Label for the folded (hidden) part of a tool run, mirroring the
 * semantic one-liners the native CLIs print: "Read 2 files", "Ran 1
 * shell command, read 2 files". Runs made up entirely of unrecognized
 * tools fall back to a generic "Called N tools".
 */
export function formatToolRunLabel(calls: ToolRunCall[]): string {
  const counts = new Map<RunCategory | "other", number>();
  for (const call of calls) {
    const category = categorizeCall(call);
    counts.set(category, (counts.get(category) ?? 0) + 1);
  }

  const known = RUN_CATEGORY_ORDER.filter((c) => counts.has(c));
  const other = counts.get("other") ?? 0;

  if (known.length === 0) {
    return `Called ${calls.length} tool${calls.length === 1 ? "" : "s"}`;
  }

  const phrases = known.map((c) => runPhrase(c, counts.get(c)!));
  if (other > 0) {
    phrases.push(`called ${other} other tool${other === 1 ? "" : "s"}`);
  }
  const label = phrases.join(", ");
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function asString(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

function asNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function verbOnly(verb: string): ToolTitle {
  return { verb, body: "" };
}

function withPath(verb: string, raw: unknown): ToolTitle | null {
  const path = asString(raw);
  return path === null ? null : { verb, body: path };
}

function sessionTitle(verb: string, args: Record<string, unknown>): ToolTitle | null {
  const tool = asString(args.tool);
  const session = asString(args.session);
  if (tool === null || session === null) return null;
  return { verb, body: `'${tool} - ${session}'` };
}

function terminalId(args: Record<string, unknown>): string | null {
  const terminal = asString(args.terminal);
  const session = asString(args.session);
  if (terminal === null || session === null) return null;
  return `${terminal}:${session}`;
}

function terminalTitle(verb: string, args: Record<string, unknown>): ToolTitle | null {
  const id = terminalId(args);
  return id === null ? null : { verb, body: `'${id}'` };
}
