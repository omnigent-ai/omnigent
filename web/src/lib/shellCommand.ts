// "Open in shell" for shell commands an agent suggests in chat: which code
// blocks qualify, the command text they hand over, and the hand-off from the
// chat (which creates the shell) to the terminal view (which pastes into it).

import { createContext, useContext } from "react";

/** Fenced-code languages whose contents are commands for a POSIX-style shell. */
const SHELL_LANGUAGES = new Set([
  "bash",
  "sh",
  "shell",
  "shellscript",
  "zsh",
  "ksh",
  "fish",
  "console",
  "shellsession",
  "shell-session",
]);

/**
 * Whether a fenced code block's ``language-*`` class marks it as shell input.
 *
 * @param className The rendered ``<code>`` element's class list, e.g.
 *   ``"language-bash"``; undefined for an unlabeled block.
 */
export function isShellCodeClassName(className: string | undefined): boolean {
  if (!className) return false;
  return className
    .split(/\s+/)
    .some(
      (token) =>
        token.startsWith("language-") &&
        SHELL_LANGUAGES.has(token.slice("language-".length).toLowerCase()),
    );
}

const PROMPT_PREFIX = /^\s*\$ /;

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
 * A transcript keeps only its ``$ `` lines (and their continuations), without
 * the prompt. Trailing newlines and control characters other than tab are dropped.
 *
 * @param code The code block's text, e.g. ``"$ npm test\\nok\\n"``.
 * @returns The command, e.g. ``"npm test"``; empty when nothing remains.
 */
export function shellCommandFromCodeBlock(code: string): string {
  const lines = withoutControlCharacters(code.replace(/\r\n?/g, "\n")).split("\n");
  let commandLines = lines;
  if (lines.some((line) => PROMPT_PREFIX.test(line))) {
    commandLines = [];
    let continued = false;
    for (const line of lines) {
      const prompted = PROMPT_PREFIX.test(line);
      if (!prompted && !continued) continue;
      commandLines.push(prompted ? line.replace(PROMPT_PREFIX, "") : line);
      continued = line.trimEnd().endsWith("\\");
    }
  }
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

function liveEntry(key: string, now: number): PendingShellInput | null {
  const entry = pendingShellInputs.get(key);
  if (entry === undefined) return null;
  if (now - entry.queuedAt > PENDING_SHELL_INPUT_TTL_MS) {
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
