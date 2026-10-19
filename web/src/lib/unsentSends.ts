// Sends this tab hasn't seen answered, per session, in sessionStorage. The server can
// hold a message POST for minutes (a sandbox launch or wake) without persisting it,
// so a reload in that window would otherwise lose the text.
import { readComposerDraft, type StoredReplyDraft } from "./replyDraft";

export interface UnsentSend {
  stableId: string;
  text: string;
  replyDraft?: StoredReplyDraft;
  /** The sender (`getCurrentAuthorId`), so another account in this tab can't recover it. */
  authorId: string | null;
}

const UNSENT_SENDS_KEY = "omnigent.unsentSends";

function read(): Record<string, UnsentSend> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.sessionStorage.getItem(UNSENT_SENDS_KEY);
    const entries: unknown = raw ? JSON.parse(raw) : {};
    if (typeof entries !== "object" || entries === null || Array.isArray(entries)) return {};
    const sends: Record<string, UnsentSend> = {};
    for (const [conversationId, entry] of Object.entries(entries)) {
      const draft = readComposerDraft(entry);
      if (!draft?.text || typeof entry !== "object" || entry === null) continue;
      const { stableId, authorId } = entry as { stableId?: unknown; authorId?: unknown };
      if (typeof stableId !== "string") continue;
      sends[conversationId] = {
        ...draft,
        stableId,
        authorId: typeof authorId === "string" ? authorId : null,
      };
    }
    return sends;
  } catch {
    return {};
  }
}

function write(sends: Record<string, UnsentSend>): void {
  if (typeof window === "undefined") return;
  try {
    if (Object.keys(sends).length === 0) window.sessionStorage.removeItem(UNSENT_SENDS_KEY);
    else window.sessionStorage.setItem(UNSENT_SENDS_KEY, JSON.stringify(sends));
  } catch {
    // Storage full or unavailable: a reload mid-send loses the text, as before.
  }
}

/** Record a message about to be POSTed; the latest send per session wins. */
export function rememberUnsentSend(conversationId: string, send: UnsentSend): void {
  write({ ...read(), [conversationId]: send });
}

/** Drop a session's record once this tab has seen that send settle. */
export function forgetUnsentSend(conversationId: string, stableId: string): void {
  const sends = read();
  if (sends[conversationId]?.stableId !== stableId) return;
  write(Object.fromEntries(Object.entries(sends).filter(([id]) => id !== conversationId)));
}

/** The send this tab last left unanswered in a session, if any. */
export function peekUnsentSend(conversationId: string): UnsentSend | null {
  return read()[conversationId] ?? null;
}

export function clearUnsentSends(): void {
  write({});
}
