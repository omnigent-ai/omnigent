import type { TicketAnswerReceipt } from "@/lib/projectTicketsApi";
import { relativeTime } from "@/lib/relativeTime";

/** How long an answer waits for its asker before Nudge is offered; matches the server. */
export const NUDGE_AFTER_MS = 10 * 60 * 1000;

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function ago(seconds: number, nowMs: number): string {
  const text = relativeTime(seconds * 1000, nowMs);
  return text === "now" ? "just now" : `${text} ago`;
}

/**
 * The receipt's second line, from stored delivery state only: a reply is a pickup, a
 * hand-off is "sent", and the asker's live status says whether it's working now.
 */
export function answerReceiptLine(
  receipt: TicketAnswerReceipt,
  asker: string,
  nowMs: number,
): string {
  switch (receipt.delivery) {
    case "replied":
      return capitalize(
        `${asker} picked it up · replied ${ago(receipt.replied_at ?? receipt.answered_at, nowMs)}`,
      );
    case "sent":
      return receipt.asker_status === "running"
        ? `Sent to ${asker} · working now`
        : `Sent to ${asker} · not picked up yet`;
    case "escalated":
      return `No reply from ${asker} yet · the Lead was told`;
    default:
      return "Not delivered yet";
  }
}

/** Whether Nudge applies: no reply yet, a session to nudge, and the timeout has passed. */
export function canNudge(receipt: TicketAnswerReceipt, nowMs: number): boolean {
  return (
    receipt.delivery !== "replied" &&
    receipt.asker_session_id !== null &&
    nowMs - receipt.answered_at * 1000 >= NUDGE_AFTER_MS
  );
}
