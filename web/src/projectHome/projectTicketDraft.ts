import type { ProjectTicket } from "@/lib/projectTicketsApi";
import {
  appendReplyQuote,
  restoreReplyDraft,
  serializeReplyDraft,
  snapshotReplyDraft,
} from "@/lib/replyDraft";
import { getSessionDraft, setSessionDraft } from "@/lib/sessionDrafts";

/** An explicit Ask action adds a removable quote without replacing the session's draft. */
export function addTicketToSessionDraft(
  sessionId: string,
  ticket: Pick<ProjectTicket, "id" | "project_id" | "title">,
) {
  const draft = getSessionDraft(sessionId) ?? { text: "", files: [] };
  const restored = restoreReplyDraft(draft.text, draft.replyDraft);
  const reference = `Ticket ID: ${ticket.id}\nProject ID: ${ticket.project_id}`;
  if (restored.quotes.some((quote) => quote.text.endsWith(reference))) return;
  const next = appendReplyQuote(restored, `${ticket.title}\n${reference}`);
  setSessionDraft(sessionId, {
    ...draft,
    text: serializeReplyDraft(next),
    replyDraft: snapshotReplyDraft(next),
  });
}
