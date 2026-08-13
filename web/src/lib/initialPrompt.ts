// Shared helpers for the "first message" a freshly created session is seeded
// with. A leaf module (no heavy imports) so both the New Chat composer and the
// Agents-rail AddAgentDialog can reuse the sanitizer.

/**
 * Sanitize a user-typed initial prompt before it is sent.
 *
 * Strips C0/C1 control characters that could corrupt a terminal
 * agent's input when the runner injects the text via ``tmux
 * send-keys`` (Claude Code / Codex native), while preserving newlines
 * (``\n``) and tabs (``\t``) so multi-line prompts survive. Mirrors
 * openui's server-side terminal-input sanitization. Trailing/leading
 * whitespace is trimmed so a whitespace-only prompt collapses to "".
 *
 * @param prompt Raw textarea value the user typed, e.g.
 *   ``"read the README\nand summarize"``.
 * @returns The sanitized prompt; ``""`` when there's nothing to send.
 */
export function sanitizeInitialPrompt(prompt: string): string {
  // Strip C0/C1 control chars except \t and \n (needed for multi-line prompts).
  // The class is the point of the rule, so allow the control regex.
  // eslint-disable-next-line no-control-regex
  return prompt.replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, "").trim();
}

/**
 * Compose the first message for an "Ask sub-agent" child from a selection in a
 * chat response: the exact selected text as a Markdown blockquote, an optional
 * surrounding excerpt (the nearest containing block), then the user's question.
 * The source can itself be a sub-agent, so the wording says "source response".
 *
 * Every context line is prefixed with ``> ``. The "Surrounding excerpt" section
 * is omitted when ``surroundingExcerpt`` is ``null`` / empty (e.g. it equalled
 * the selection). Arguments should already be sanitized.
 *
 * @param selectedText The exact highlighted text.
 * @param surroundingExcerpt The nearest containing block, or ``null`` to omit.
 * @param question The user's question about the selection.
 * @returns The composed prompt.
 */
export function composeAskSubagentPrompt(
  selectedText: string,
  surroundingExcerpt: string | null,
  question: string,
): string {
  const quote = (text: string) =>
    text
      .split("\n")
      .map((line) => `> ${line}`)
      .join("\n");
  const parts = [
    "Selected from the source response:",
    "",
    "Selected text:",
    "",
    quote(selectedText),
  ];
  if (surroundingExcerpt !== null && surroundingExcerpt !== "") {
    parts.push("", "Surrounding excerpt:", "", quote(surroundingExcerpt));
  }
  parts.push("", `Question: ${question}`);
  return parts.join("\n");
}
