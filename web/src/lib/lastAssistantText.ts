// Fetch + extract the agent's final message text for a notification preview.
//
// When a turn ends we want the OS notification body to show the first few
// lines the agent actually wrote ("Sure — I've fixed the badge bug and…")
// instead of the generic "Agent finished and is ready for your input." The
// conversations LIST endpoint that drives `useIdleNotifications` carries no
// message text, so on a turn-end transition we make one extra, best-effort
// fetch of the session's most recent items and pull the last assistant
// `output_text` out of them.
//
// Everything here is defensive and non-throwing: a failed fetch, a session
// that ended on a tool call / elicitation (no trailing assistant text), or an
// unexpected wire shape all resolve to `undefined`, and the caller falls back
// to the generic body. This module never takes down the notification path.

/**
 * How many trailing items to scan for the last assistant message. A turn
 * usually ends on the assistant's final message, but it may be followed by a
 * tool/native-tool item or two, so we look a little way back rather than only
 * at the single newest item.
 */
const SCAN_ITEMS = 12;

/** Default cap for the preview body (chars) — enough for ~3 lines of banner. */
const DEFAULT_MAX_CHARS = 160;

/** Default cap on the number of lines surfaced in the preview. */
const DEFAULT_MAX_LINES = 3;

/**
 * Extract the concatenated `output_text` from a single raw session item,
 * or `undefined` when the item isn't an assistant message with text.
 *
 * Pure and shape-tolerant: the items API returns each item as the server's
 * flattened wire dict (`{ type, role, content: [...] }`), so we duck-type
 * rather than depend on a parsed model. Only `output_text` blocks count —
 * an assistant turn that produced just tool calls yields `undefined`.
 *
 * :param item: One raw item from `GET /v1/sessions/{id}/items`.
 * :returns: The assistant text, or `undefined`.
 */
export function extractAssistantText(item: unknown): string | undefined {
  if (item === null || typeof item !== "object") return undefined;
  const record = item as Record<string, unknown>;
  if (record.type !== "message" || record.role !== "assistant") return undefined;
  const content = record.content;
  if (!Array.isArray(content)) return undefined;
  const parts: string[] = [];
  for (const block of content) {
    if (block === null || typeof block !== "object") continue;
    const b = block as Record<string, unknown>;
    if (b.type === "output_text" && typeof b.text === "string") parts.push(b.text);
  }
  const joined = parts.join("").trim();
  return joined.length > 0 ? joined : undefined;
}

/**
 * Condense raw assistant text into a short notification body: the first few
 * non-empty lines, capped at a character budget with an ellipsis.
 *
 * Markdown is left as-is (notification bodies render plain text, so `**bold**`
 * just shows its asterisks — acceptable for a preview and not worth a parser).
 *
 * :param text: The full assistant message text.
 * :param maxChars: Maximum characters before truncation (default 160).
 * :param maxLines: Maximum non-empty lines to include (default 3).
 * :returns: A trimmed, capped preview string.
 */
export function previewText(
  text: string,
  maxChars: number = DEFAULT_MAX_CHARS,
  maxLines: number = DEFAULT_MAX_LINES,
): string {
  const lines = text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
    .slice(0, maxLines);
  let preview = lines.join("\n");
  if (preview.length > maxChars) {
    preview = `${preview.slice(0, maxChars - 1).trimEnd()}…`;
  }
  return preview;
}

/**
 * Best-effort fetch of the agent's final message text for a session, ready to
 * use as a notification body. Returns `undefined` on any failure or when the
 * session has no trailing assistant text (e.g. it ended on a tool call), so
 * the caller can fall back to a generic message.
 *
 * Fetches the newest items (`order=desc`) and returns the FIRST assistant
 * `output_text` found scanning newest-first — i.e. the message that just
 * completed the turn.
 *
 * :param sessionId: Session/conversation id, e.g. ``"conv_abc123"``.
 * :param maxChars: Optional preview character budget (default 160).
 * :returns: A short preview string, or ``undefined``.
 */
export async function fetchLastAssistantText(
  sessionId: string,
  maxChars: number = DEFAULT_MAX_CHARS,
): Promise<string | undefined> {
  try {
    const params = new URLSearchParams({ limit: String(SCAN_ITEMS), order: "desc" });
    const res = await fetch(`/v1/sessions/${encodeURIComponent(sessionId)}/items?${params}`);
    if (!res.ok) return undefined;
    const json = (await res.json()) as { data?: unknown };
    const items = Array.isArray(json.data) ? json.data : [];
    for (const item of items) {
      const text = extractAssistantText(item);
      if (text !== undefined) return previewText(text, maxChars);
    }
    return undefined;
  } catch {
    // Network error, non-JSON body, etc. — fall back to the generic message.
    return undefined;
  }
}
