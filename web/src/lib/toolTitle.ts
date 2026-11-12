// Titles for tool-call rows: a bold `verb` plus a dynamic `body` ("Read foo.py").
// Tools without a formatter get a humanized name and key argument values, with
// the MCP server as a tag and the raw name in the tooltip.

import type { ShellOutputFormat } from "@/lib/shellToolOutput";
import { identifierWords, ownValue, sentenceCase } from "@/lib/toolValue";

/**
 * Structured title for a tool call.
 * - `verb`: the static action phrase (e.g. "Read", "Start child
 *   session:"). Rendered bold/foreground by the trigger row. `null`
 *   when there's nothing to emphasize (e.g. raw shell commands, or
 *   the fallback for unknown tools).
 * - `body`: the dynamic payload (path, command, session id). Empty
 *   string when the title is verb-only (e.g. "Read inbox").
 * - `filePath`: set when the call targets exactly one file, as the agent
 *   passed it (relative or absolute); the trigger row links it.
 * - `isShellCommand`: set when `body` is a shell command line; the
 *   trigger row renders it terminal-style (`$ cmd`, monospace).
 * - `server`: the MCP server that owns the tool (e.g. "linear"); the
 *   trigger row shows it as a tag.
 * - `toolName`: the raw tool name, set when the title was derived from it
 *   so the row's tooltip can still show the exact name.
 */
export interface ToolTitle {
  verb: string | null;
  body: string;
  filePath?: string;
  isShellCommand?: boolean;
  server?: string;
  toolName?: string;
}

type ArgFormatter = (args: Record<string, unknown>) => ToolTitle | null;

const FORMATTERS: Record<string, ArgFormatter> = {
  // OS-environment tools — drop the noisy `sys_os_*` prefix; the verb
  // alone communicates the action.
  sys_os_shell: (args) => shellTitle(asString(args.command)),
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
      ? verbOnly("Smart routing")
      : { verb: "Smart routing:", body: `${count} task${count === 1 ? "" : "s"}` };
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
    return shellTitle(asString(args.command));
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
    return shellTitle(cmd === null ? null : unwrapShellCommand(cmd));
  },
  apply_patch: (args) => {
    const changes = Array.isArray(args.changes) ? args.changes : null;
    if (changes === null || changes.length === 0) return null;
    if (changes.length === 1) {
      const only = changes[0];
      const path = only !== null && typeof only === "object" ? Reflect.get(only, "path") : null;
      const title = withPath("Edit", path);
      if (title !== null) return title;
    }
    return { verb: "Edit", body: `${changes.length} files` };
  },

  // Pi / OpenCode native harness tools (lowercase names). OpenCode
  // passes `filePath`, Pi passes `path`.
  bash: (args) => shellTitle(asString(args.command)),
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
  web_read: (args) => {
    const url = asString(args.url);
    return url === null ? null : { verb: "Web read:", body: url };
  },
};

/**
 * Compute the title shown in a tool-call trigger row. Tries the per-tool
 * formatter first; otherwise humanizes the tool name and lists its key
 * argument values, with no tool-specific knowledge.
 */
export function formatToolTitle(name: string, args: Record<string, unknown>): ToolTitle {
  const { server, tool } = splitToolName(name);
  const formatter = server === undefined ? ownValue(FORMATTERS, tool) : undefined;
  if (formatter !== undefined) {
    const title = formatter(args);
    if (title !== null && (title.verb !== null || title.body.length > 0)) {
      return title;
    }
  }
  return {
    verb: humanizeToolName(tool, server),
    body: summarizeArgs(args),
    toolName: name,
    ...(server === undefined ? {} : { server }),
  };
}

// `server__tool`, as Claude Code names MCP tools after an `mcp__` prefix and
// SDK harnesses name a spec-declared MCP server's tools without one.
const SERVER_TOOL_RE = /^([A-Za-z0-9](?:[\w-]*?[A-Za-z0-9])?)__([A-Za-z0-9][\s\S]*)$/;

/**
 * Split an MCP tool name into its server and tool: `mcp__linear__get_issue`
 * and `linear__get_issue` -> `{ server: "linear", tool: "get_issue" }`.
 * Omnigent's MCP server carries the built-in tools and proxies spec-declared
 * servers, so `mcp__omnigent__x` resolves as `x`.
 */
export function splitToolName(name: string): { server?: string; tool: string } {
  const bare = name.startsWith("mcp__") ? name.slice("mcp__".length) : name;
  const match = SERVER_TOOL_RE.exec(bare);
  if (match === null) return { tool: name };
  const [, server, tool] = match;
  if (server === "omnigent") return splitToolName(tool!);
  return { server: server!, tool: tool! };
}

// Verbs a tool name may end with (`sys_scheduled_task_create`); moved to the
// front so the label reads as an action ("Create scheduled task"). Words like
// "list" and "search" are left out: at the end of a name they are nouns.
const TRAILING_VERBS = new Set([
  "add",
  "cancel",
  "close",
  "create",
  "delete",
  "get",
  "launch",
  "read",
  "remove",
  "rename",
  "send",
  "set",
  "start",
  "stop",
  "update",
  "write",
]);
// A name that already starts with one of these reads as an action as-is.
const LEADING_VERBS = new Set([
  ...TRAILING_VERBS,
  "apply",
  "check",
  "edit",
  "execute",
  "fetch",
  "find",
  "list",
  "load",
  "manage",
  "merge",
  "open",
  "poll",
  "post",
  "query",
  "reply",
  "resolve",
  "run",
  "search",
  "submit",
]);

/**
 * A tool name as a sentence-case action label: `manage_todo_list` -> "Manage
 * todo list". Drops Omnigent's `sys_` prefix and a repeat of the server name
 * (`slack_read_api_call` on the slack server -> "Read API call").
 */
export function humanizeToolName(tool: string, server?: string): string {
  let words = identifierWords(tool);
  if (words.length > 1 && words[0] === "sys") words = words.slice(1);
  const serverWords = server === undefined ? [] : identifierWords(server);
  let shared = 0;
  while (shared < serverWords.length && shared < words.length - 1) {
    if (words[shared] !== serverWords[shared]) break;
    shared += 1;
  }
  words = words.slice(shared);
  const last = words[words.length - 1];
  if (
    words.length > 1 &&
    last !== undefined &&
    TRAILING_VERBS.has(last) &&
    !LEADING_VERBS.has(words[0]!)
  ) {
    words = [last, ...words.slice(0, -1)];
  }
  const label = sentenceCase(words);
  return label.length > 0 ? label : tool;
}

const ARG_SUMMARY_PARTS = 3;
const ARG_STRING_MAX_CHARS = 200;
const ARG_LIST_MAX_ITEMS = 3;
const ARG_LIST_ITEM_MAX_CHARS = 40;
// Numbers only identify something under id-like keys; a bare `20` from
// `limit` or `max_results` means nothing in a one-line summary.
const ID_LIKE_KEY_WORDS = new Set(["id", "number", "num", "pid", "port"]);
// Arguments that pick which fields or columns come back (`fields: [id, title]`,
// `select: "id,title"`); they shape the result rather than name what the call is about.
const SELECTOR_KEY_WORDS = new Set([
  "attributes",
  "columns",
  "expand",
  "fields",
  "properties",
  "select",
]);
// Words that mark an argument as a credential; matched per word so `author` passes.
const SECRET_KEY_WORDS = new Set([
  "apikey",
  "auth",
  "authorization",
  "bearer",
  "cookie",
  "credential",
  "credentials",
  "key",
  "passphrase",
  "passwd",
  "password",
  "pat",
  "private",
  "secret",
  "signature",
  "token",
]);

/**
 * Key argument values for a collapsed row, joined with " · "; skips long,
 * multi-line, nested and secret-looking values.
 */
export function summarizeArgs(args: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(args)) {
    if (parts.length >= ARG_SUMMARY_PARTS) break;
    if (identifierWords(key).some((word) => SECRET_KEY_WORDS.has(word))) continue;
    const part = argSummaryPart(key, value);
    if (part !== null) parts.push(part);
  }
  return parts.join(" · ");
}

function argSummaryPart(key: string, value: unknown): string | null {
  const lastWord = identifierWords(key).at(-1);
  if (lastWord !== undefined && SELECTOR_KEY_WORDS.has(lastWord)) return null;
  if (typeof value === "string") {
    const text = value.trim();
    if (text.length === 0 || text.length > ARG_STRING_MAX_CHARS || text.includes("\n")) return null;
    return text;
  }
  if (typeof value === "number") {
    return lastWord !== undefined && ID_LIKE_KEY_WORDS.has(lastWord) ? String(value) : null;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return null;
    const shortStrings =
      value.length <= ARG_LIST_MAX_ITEMS &&
      value.every((item) => typeof item === "string" && item.length <= ARG_LIST_ITEM_MAX_CHARS);
    if (shortStrings) return value.join(", ");
    return `${value.length} ${value.length === 1 ? "item" : "items"}`;
  }
  return null;
}

/**
 * Action categories for the collapsed tool-run summary line. Names cover
 * the omnigent sys_* tools plus the native harness CLIs (Claude Code's
 * Bash/Read/Edit..., Codex's shell/apply_patch). Shell calls whose
 * command is a bare `ls` re-categorize as directory listings, matching
 * the Claude Code TUI's step summaries.
 */
export type RunCategory = "shell" | "list" | "read" | "edit" | "search";

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

/** A tool name without Omnigent's `mcp__omnigent__` prefix. */
export function stripOmnigentPrefix(name: string): string {
  return name.startsWith("mcp__omnigent__") ? name.slice("mcp__omnigent__".length) : name;
}

// Login-shell wrapper Codex puts around every command, e.g.
// `/bin/bash -lc 'cat notes.txt'` or `/bin/zsh -lc ls`.
const SHELL_WRAPPER_RE = /^(?:\S+\/)?(?:bash|zsh|sh|dash|fish)\s+-l?c\s+([\s\S]+)$/;

/**
 * Peel a login-shell wrapper (and its quoting) off a command string. Anything
 * after the wrapper that is not exactly one shell word, e.g.
 * `sh -c 'a' && echo 'b'`, is returned unchanged.
 */
function unwrapShellCommand(command: string): string {
  const trimmed = command.trim();
  const match = SHELL_WRAPPER_RE.exec(trimmed);
  if (match === null) return trimmed;
  const inner = decodeShellWord(match[1]!.trim())?.trim();
  return inner ? inner : trimmed;
}

/** Decode one shell word (`'it'\''s'`, `"a \"b\""`, `ls`), or `null` if it is not exactly one. */
function decodeShellWord(text: string): string | null {
  const piece = /'([^']*)'|"((?:[^"\\]|\\[\s\S])*)"|\\([\s\S])|([^\s'"\\]+)/y;
  let decoded = "";
  while (piece.lastIndex < text.length) {
    const match = piece.exec(text);
    if (match === null) return null;
    const [, singleQuoted, doubleQuoted, escaped, bare] = match;
    if (doubleQuoted !== undefined) {
      decoded += doubleQuoted.replace(/\\([\\"$`])/g, "$1");
    } else {
      decoded += singleQuoted ?? escaped ?? bare;
    }
  }
  return decoded;
}

/** Whether `name` is a shell-command tool in any harness (Bash, shell, sys_os_shell...). */
export function isShellTool(name: string): boolean {
  return ownValue(RUN_CATEGORIES, stripOmnigentPrefix(name)) === "shell";
}

/** A shell tool call: the command line it ran and how its output is shaped. */
export interface ShellToolCall {
  command: string;
  outputFormat: ShellOutputFormat;
}

const SHELL_OUTPUT_FORMATS: Record<string, ShellOutputFormat> = {
  sys_os_shell: "sys-os-json",
  shell: "codex-suffix",
};

/**
 * Describe a shell tool call, or return `null` when the call is not a shell
 * tool or carries no string `command`. Only Codex's `shell` adds a
 * login-shell wrapper, so other harnesses' commands are shown as run.
 */
export function shellToolCall(name: string, args: Record<string, unknown>): ShellToolCall | null {
  if (!isShellTool(name)) return null;
  const command = asString(args.command);
  if (command === null) return null;
  const bareName = stripOmnigentPrefix(name);
  return {
    command: bareName === "shell" ? unwrapShellCommand(command) : command,
    outputFormat: ownValue(SHELL_OUTPUT_FORMATS, bareName) ?? "plain",
  };
}

// Leading `NAME=value` assignments, with quoted values, e.g.
// `PATH="$HOME/bin:$PATH" FOO=1 make` -> `PATH="$HOME/bin:$PATH" FOO=1 `.
// Unquoted values stop at quotes, parens and shell operators, so neither
// `FOO="a b"c` nor `FOO=a;b` is split inside a value.
const ENV_PREFIX_RE =
  /^(?:[A-Za-z_][A-Za-z0-9_]*=(?:"(?:[^"\\]|\\.)*"|'[^']*'|[^\s"'()`;&|<>]*)\s+)+/;
const ASSIGNMENT_RE = /^[A-Za-z_][A-Za-z0-9_]*=/;

/**
 * Split leading environment assignments off a command so the row can dim
 * them and let the program name stand out. `env` keeps its trailing
 * whitespace; `env + rest` always equals the input.
 */
export function splitEnvPrefix(command: string): { env: string; rest: string } {
  const match = ENV_PREFIX_RE.exec(command);
  if (match === null) return { env: "", rest: command };
  const rest = command.slice(match[0].length);
  // Assignments with no program after them (`FOO=1 BAR=2`) are not a prefix.
  if (rest.length === 0 || ASSIGNMENT_RE.test(rest)) return { env: "", rest: command };
  return { env: match[0], rest };
}

/**
 * A script's first line for a collapsed row, and how many more non-blank lines
 * follow it. Leading comment lines are skipped: the first line is always top
 * level, so a `#` there starts a comment.
 */
export function shellCommandPreview(script: string): { firstLine: string; moreLines: number } {
  const lines = script
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
  const shown = lines.find((line) => !line.startsWith("#")) ?? lines[0] ?? "";
  return { firstLine: shown.replace(/\s*\\$/, ""), moreLines: Math.max(0, lines.length - 1) };
}

/** The action category of a tool call, e.g. `Bash("ls")` -> "list". */
export function toolCallCategory(call: ToolRunCall): RunCategory | "other" {
  const name = stripOmnigentPrefix(call.name);
  const category = ownValue(RUN_CATEGORIES, name) ?? "other";
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
    const category = toolCallCategory(call);
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

function shellTitle(command: string | null): ToolTitle | null {
  return command === null ? null : { verb: null, body: command, isShellCommand: true };
}

function verbOnly(verb: string): ToolTitle {
  return { verb, body: "" };
}

function withPath(verb: string, raw: unknown): ToolTitle | null {
  const path = asString(raw);
  return path === null ? null : { verb, body: path, filePath: path };
}

function sessionTitle(verb: string, args: Record<string, unknown>): ToolTitle | null {
  // The named spawn schema (omnigent/tools/builtins/spawn.py) sends `agent`/`title`.
  const tool = asString(args.tool) ?? asString(args.agent);
  const session = asString(args.session) ?? asString(args.title);
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
