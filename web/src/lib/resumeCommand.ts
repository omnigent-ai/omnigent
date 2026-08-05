/**
 * Build the `omnigent resume` command that reattaches a terminal to a
 * session opened in the browser.
 *
 * `omnigent resume <id>` looks the conversation up, reads its
 * `omnigent.wrapper` label, and hands off to the matching native
 * wrapper. `--server` is carried explicitly — the browser already knows
 * which server it is talking to, and naming it resumes against that
 * same server whether it is local or deployed.
 *
 * Emitted as ONE line: unlike the reconnect dialog's backslash-wrapped
 * form, this string is only ever copied (never rendered in a narrow
 * box), so it should paste into a shell as a single command.
 */
export function buildResumeCommand({
  conversationId,
  serverUrl,
}: {
  conversationId: string;
  serverUrl: string;
}): string {
  return `omnigent resume ${conversationId} --server ${serverUrl}`;
}
