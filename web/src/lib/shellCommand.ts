// "Open in shell" for shell commands an agent suggests in chat: which code
// blocks qualify, the command text they hand over, and the hand-off from the
// chat (which creates the shell) to the terminal view (which pastes into it).

import { createContext, useContext } from "react";

/** Fenced-code languages for terminal sessions: prompts mixed with output. */
const SHELL_SESSION_LANGUAGES = new Set(["console", "shellsession", "shell-session"]);
/** Fenced-code languages whose contents are commands for an interactive shell. */
const SHELL_LANGUAGES = new Set([
  "bash",
  "sh",
  "shell",
  "shellscript",
  "zsh",
  "ksh",
  "fish",
  ...SHELL_SESSION_LANGUAGES,
]);

function hasLanguage(className: string | undefined, languages: Set<string>): boolean {
  if (!className) return false;
  return className
    .split(/\s+/)
    .some(
      (token) =>
        token.startsWith("language-") &&
        languages.has(token.slice("language-".length).toLowerCase()),
    );
}

/**
 * Whether a fenced code block's ``language-*`` class marks it as shell input.
 *
 * @param className The rendered ``<code>`` element's class list, e.g.
 *   ``"language-bash"``; undefined for an unlabeled block.
 */
export function isShellCodeClassName(className: string | undefined): boolean {
  return hasLanguage(className, SHELL_LANGUAGES);
}

/**
 * Whether the block is a terminal session (``console`` and the like), whose
 * lines are output unless they follow a ``$ `` prompt.
 *
 * @param className The rendered ``<code>`` element's class list, e.g.
 *   ``"language-console"``.
 */
export function isShellSessionClassName(className: string | undefined): boolean {
  return hasLanguage(className, SHELL_SESSION_LANGUAGES);
}

const FIRST_PROMPT = /^(\s*)\$ /;
// The shell's secondary prompt (PS2), shown before each continuation line.
const CONTINUATION_PREFIX = "> ";
const SECONDARY_PROMPT_LINE = /^\s*>(?:\s|$)/;
// A here-document (not a ``<<<`` here-string), or a line ending in a pipe,
// ``&&`` or ``||``: the command goes on past its first line.
const HEREDOC_OPERATOR = /(?<!<)<<(?!<)/;
const TRAILING_OPERATOR = /(?:\|&?|&&)\s*$/;
// A compound command whose body would be on the next lines.
const TRAILING_KEYWORD = /(?:^|[\s;&|()])(?:do|then|else|elif|in)\s*$/;
// ANSI-C quoting, where ``\'`` doesn't end the string.
const ANSI_C_QUOTE = "$'";

/** Whether a line ends in an unescaped backslash, which joins the next line. */
function endsWithLineContinuation(line: string): boolean {
  const trailing = /\\*$/.exec(line)?.[0].length ?? 0;
  return trailing % 2 === 1;
}

/**
 * Whether a command leaves a quote, ``(`` or ``{`` open (bash rules, ``#``
 * comments skipped).
 */
function isUnclosed(command: string): boolean {
  let quote: "'" | '"' | null = null;
  let escaped = false;
  let inComment = false;
  let groupDepth = 0;
  for (let index = 0; index < command.length; index++) {
    const char = command[index];
    if (inComment) {
      if (char === "\n") inComment = false;
    } else if (quote === "'") {
      if (char === "'") quote = null;
    } else if (escaped) {
      escaped = false;
    } else if (char === "\\") {
      escaped = true;
    } else if (quote === '"') {
      if (char === '"') quote = null;
    } else if (char === "'" || char === '"') {
      quote = char;
    } else if (char === "#" && (index === 0 || /[\s;&|()<>]/.test(command[index - 1]))) {
      inComment = true;
    } else if (char === "(" || char === "{") {
      groupDepth++;
    } else if (char === ")" || char === "}") {
      groupDepth--;
    }
  }
  return quote !== null || groupDepth > 0;
}

/**
 * Whether a prompted command may continue in ways the transcript can't show
 * apart from output, so pasting it could drop lines or promote output.
 */
function isAmbiguousCommand(lines: string[]): boolean {
  // Trailing blank lines are dropped before pasting, so a final backslash shows.
  const command = lines.join("\n").trimEnd();
  return (
    HEREDOC_OPERATOR.test(command) ||
    TRAILING_OPERATOR.test(command) ||
    TRAILING_KEYWORD.test(command) ||
    endsWithLineContinuation(command) ||
    command.includes(ANSI_C_QUOTE) ||
    isUnclosed(command) ||
    // A "#" may comment out a backslash that seemed to join the next line.
    (lines.length > 1 && command.includes("#"))
  );
}

/**
 * The prompted commands of a transcript, or null when one can't be safely
 * told apart from its output.
 */
function transcriptCommands(lines: string[], indent: string): string[] | null {
  const prompt = `${indent}$ `;
  const commands: string[][] = [];
  // The command the previous line belonged to, or null after output.
  let current: string[] | null = null;
  for (const line of lines) {
    if (line.startsWith(prompt)) {
      current = [line.slice(prompt.length)];
      commands.push(current);
    } else if (current !== null && endsWithLineContinuation(current[current.length - 1])) {
      const unindented = line.startsWith(indent) ? line.slice(indent.length) : line;
      // Left in, "> " would be a redirection that truncates a file.
      current.push(
        unindented.startsWith(CONTINUATION_PREFIX)
          ? unindented.slice(CONTINUATION_PREFIX.length)
          : unindented,
      );
    } else if (current !== null && SECONDARY_PROMPT_LINE.test(line)) {
      // Right after a command this is either its next line (a pipeline, loop,
      // quote or here-document) or output that starts with ">".
      return null;
    } else {
      current = null;
    }
  }
  if (commands.some(isAmbiguousCommand)) return null;
  return commands.map((command) => command.join("\n"));
}

/** Whether a UTF-16 code unit is a control character other than tab or LF. */
function isDroppedControl(code: number): boolean {
  return (code < 0x20 && code !== 0x09 && code !== 0x0a) || (code >= 0x7f && code <= 0x9f);
}

// ESC would let a block end the shell's bracketed paste early (``ESC[201~``)
// and run whatever follows; the bridge strips the same characters.
function withoutControlCharacters(text: string): string {
  let kept = "";
  for (const char of text) {
    if (!isDroppedControl(char.charCodeAt(0))) kept += char;
  }
  return kept;
}

/**
 * The command text a shell code block hands to a new shell.
 *
 * A block whose first non-blank line starts with ``$ `` is a transcript: only
 * its ``$ `` lines (at that line's indent) are kept, without the prompts, plus
 * lines joined by a trailing backslash, without a ``> `` secondary prompt.
 * A transcript whose commands can't be safely told apart from their output
 * (a here-document, open quote or trailing operator, or a ``>`` line right
 * after a command) yields nothing. Trailing newlines and control characters
 * other than tab are dropped.
 *
 * @param code The code block's text, e.g. ``"$ npm test\\nok\\n"``.
 * @param sessionTranscript The block is a terminal session (see
 *   :func:`isShellSessionClassName`), so without a ``$ `` prompt nothing in it
 *   is known to be a command.
 * @returns The command, e.g. ``"npm test"``; empty when nothing remains.
 */
export function shellCommandFromCodeBlock(code: string, sessionTranscript = false): string {
  const lines = withoutControlCharacters(code.replace(/\r\n?/g, "\n")).split("\n");
  const firstLine = lines.find((line) => line.trim() !== "");
  const firstPrompt = firstLine === undefined ? null : FIRST_PROMPT.exec(firstLine);
  if (firstPrompt === null && sessionTranscript) return "";
  const commandLines = firstPrompt === null ? lines : transcriptCommands(lines, firstPrompt[1]);
  if (commandLines === null) return "";
  return commandLines
    .join("\n")
    .replace(/^(?:[ \t]*\n)+/, "")
    .trimEnd();
}

/** Opens a new shell in the session on screen with ``command`` pasted at its prompt. */
export type ShellCommandOpener = (command: string) => void;

/** Provided around the session chat when the viewer may open a shell there. */
export const ShellCommandOpenerContext = createContext<ShellCommandOpener | null>(null);

/** The session's shell-command opener, or null where no shell can be opened. */
export function useShellCommandOpener(): ShellCommandOpener | null {
  return useContext(ShellCommandOpenerContext);
}

// A queued command waits for its shell's terminal view to attach. A shell that
// never attaches within this window (closed, or the page navigated away) drops
// its command, so a much later attach never pastes stale text.
export const PENDING_SHELL_INPUT_TTL_MS = 60_000;

interface PendingShellInput {
  text: string;
  queuedAt: number;
}

const pendingShellInputs = new Map<string, PendingShellInput>();
const pendingShellInputListeners = new Set<() => void>();

function pendingShellInputKey(sessionId: string, terminalId: string): string {
  return JSON.stringify([sessionId, terminalId]);
}

function isExpired(entry: PendingShellInput, now: number): boolean {
  return now - entry.queuedAt > PENDING_SHELL_INPUT_TTL_MS;
}

function liveEntry(key: string, now: number): PendingShellInput | null {
  const entry = pendingShellInputs.get(key);
  if (entry === undefined) return null;
  if (isExpired(entry, now)) {
    pendingShellInputs.delete(key);
    return null;
  }
  return entry;
}

/**
 * Queue ``text`` to be pasted into a shell once its terminal view is ready.
 *
 * @param sessionId Session the shell belongs to, e.g. ``"conv_abc123"``.
 * @param terminalId The shell's terminal resource id, e.g. ``"terminal_zsh_u-1a2b3c"``.
 * @param text The command to paste, without a trailing newline.
 */
export function queuePendingShellInput(
  sessionId: string,
  terminalId: string,
  text: string,
  now = Date.now(),
): void {
  // Shells that never attached would otherwise keep their commands forever.
  for (const [key, entry] of pendingShellInputs) {
    if (isExpired(entry, now)) pendingShellInputs.delete(key);
  }
  pendingShellInputs.set(pendingShellInputKey(sessionId, terminalId), { text, queuedAt: now });
  for (const listener of pendingShellInputListeners) listener();
}

/** Whether a shell has a live queued command (without consuming it). */
export function hasPendingShellInput(
  sessionId: string,
  terminalId: string,
  now = Date.now(),
): boolean {
  return liveEntry(pendingShellInputKey(sessionId, terminalId), now) !== null;
}

/**
 * Take a shell's queued command so exactly one terminal view pastes it.
 *
 * @returns The command, or null when none is queued, it expired, or another
 *   view already took it.
 */
export function claimPendingShellInput(
  sessionId: string,
  terminalId: string,
  now = Date.now(),
): string | null {
  const key = pendingShellInputKey(sessionId, terminalId);
  const entry = liveEntry(key, now);
  pendingShellInputs.delete(key);
  return entry?.text ?? null;
}

/** Subscribe to newly queued commands; returns the unsubscribe function. */
export function subscribePendingShellInput(listener: () => void): () => void {
  pendingShellInputListeners.add(listener);
  return () => {
    pendingShellInputListeners.delete(listener);
  };
}
