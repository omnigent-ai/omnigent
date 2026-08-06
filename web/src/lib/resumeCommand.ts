// One line so it pastes straight into a shell; `--server` is explicit so the
// command resumes against whichever server the browser is talking to.
export function buildResumeCommand({
  conversationId,
  serverUrl,
}: {
  conversationId: string;
  serverUrl: string;
}): string {
  return `omnigent resume ${conversationId} --server ${serverUrl}`;
}
