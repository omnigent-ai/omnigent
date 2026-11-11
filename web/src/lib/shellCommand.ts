/** "Run in shell" for chat code blocks: open a new shell and run the block in it. */

import { createContext, useContext } from "react";

const SHELL_LANGUAGES = new Set(["bash", "sh", "shell", "shellscript", "zsh"]);

/**
 * Whether a code block's ``className`` marks it as a shell script.
 *
 * ``console`` blocks are left out: they mix prompts with output, and the
 * output lines would run too.
 *
 * :param className: The ``<code>`` element's class, e.g. ``"language-bash"``.
 * :returns: True for bash, sh, shell, shellscript and zsh blocks.
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

// Editing controls (all C0/C1 but tab and newline) would rewrite the line, so
// the shell would run something other than the visible text.
// oxlint-disable-next-line no-control-regex
const CONTROL_CHARACTERS = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/g;

/**
 * The keystrokes that run *code* at a shell prompt: each line followed by Enter.
 *
 * Only the block's trailing newlines are dropped, so trailing spaces that are
 * part of a command (an escaped space before the line end) are kept.
 *
 * :param code: The code block's text, e.g. ``"cd web\npnpm test\n"``.
 * :returns: The text with newlines sent as Enter, e.g. ``"cd web\rpnpm test\r"``,
 *     or ``""`` when the block is blank.
 */
export function shellCommandKeystrokes(code: string): string {
  const lines = code.replace(/\r\n?/g, "\n").replace(CONTROL_CHARACTERS, "").replace(/\n+$/, "");
  return lines.trim() === "" ? "" : `${lines.replace(/\n/g, "\r")}\r`;
}

/** Runs a code block's text in a new shell; ``null`` where the viewer can't open one. */
export type ShellCommandRunner = (code: string) => void;

export const ShellCommandRunnerContext = createContext<ShellCommandRunner | null>(null);

export function useShellCommandRunner(): ShellCommandRunner | null {
  return useContext(ShellCommandRunnerContext);
}

// Keystrokes waiting for a just-created shell's terminal view to reach its
// shell, keyed by session and terminal id. The view takes them on the first
// terminal output, so they're sent once even if the view re-attaches.
const queuedKeystrokes = new Map<string, string>();

function queueKey(sessionId: string, terminalId: string): string {
  return `${sessionId}\u0000${terminalId}`;
}

/** Queue *keystrokes* for the terminal *terminalId* of session *sessionId*. */
export function queueShellKeystrokes(
  sessionId: string,
  terminalId: string,
  keystrokes: string,
): void {
  queuedKeystrokes.set(queueKey(sessionId, terminalId), keystrokes);
}

/** Drop any keystrokes still queued for a terminal, e.g. after its create failed. */
export function dropShellKeystrokes(sessionId: string, terminalId: string): void {
  queuedKeystrokes.delete(queueKey(sessionId, terminalId));
}

/** Take (and forget) the keystrokes queued for a terminal, or ``null``. */
export function takeShellKeystrokes(sessionId: string, terminalId: string): string | null {
  const key = queueKey(sessionId, terminalId);
  const keystrokes = queuedKeystrokes.get(key);
  if (keystrokes === undefined) return null;
  queuedKeystrokes.delete(key);
  return keystrokes;
}
