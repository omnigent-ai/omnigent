// Folds an orchestrator's automatic turns (ticket updates, messages from other
// sessions, system markers such as timers) into one "background updates" row per
// run, so the person's own conversation stays readable. Display-only: the agent
// still sees every turn, and a turn that asks the person something stays visible.

import { isTextBlock } from "./blocks";
import type { Bubble, RenderItem } from "./renderItems";
import { splitSessionMessageOrigin } from "./sessionMessageOrigin";
import { isSystemUserContent, stripAutomationContext } from "./systemMessage";

/** Openings of the server's ticket notifications (``ticket_batch_message``). */
const TICKET_UPDATE_PREFIXES = [
  "Automatic ticket update for project ",
  "Ticket update digest for project ",
];
// A full event section ("Ticket <id> · <kind>") or a digest line ("- <id> \"title\": …").
const TICKET_ID_RE = /^(?:Ticket ([0-9a-f]{32}) · |- ([0-9a-f]{32}) ")/gm;
// An urgent update section: its header line, then its payload as one JSON line.
const TICKET_UPDATED_RE = /^Ticket [0-9a-f]{32} · updated\n(\{.*\})$/gm;

export type AutomaticInputKind = "ticket_update" | "session_message" | "system";

type UserBubble = Extract<Bubble, { kind: "user" }>;
export type BackgroundUpdatesBubble = Extract<Bubble, { kind: "background_updates" }>;

function userText(bubble: UserBubble): string {
  return stripAutomationContext(
    bubble.content
      .filter(isTextBlock)
      .map((block) => block.text)
      .join(""),
  );
}

/** What started *bubble*, when no person wrote it; null for a person's message. */
export function automaticInputKind(bubble: Bubble): AutomaticInputKind | null {
  if (bubble.kind !== "user" || bubble.pending || bubble.shellCommand !== undefined) return null;
  const text = userText(bubble);
  if (TICKET_UPDATE_PREFIXES.some((prefix) => text.startsWith(prefix))) return "ticket_update";
  if (splitSessionMessageOrigin(text) !== null) return "session_message";
  if (isSystemUserContent(bubble.content)) return "system";
  return null;
}

function addsNeedsHuman(args: Record<string, unknown>): boolean {
  const labels = args.labels;
  return (
    Array.isArray(labels) &&
    labels.some(
      (label) =>
        typeof label === "object" &&
        label !== null &&
        (label as { key?: unknown }).key === "needs-human",
    )
  );
}

/**
 * Whether *item* asks the person for something: an Inbox post, a ticket marked
 * ``needs-human``, or a question still waiting for an answer.
 */
export function itemAsksHuman(item: RenderItem): boolean {
  if (item.kind === "elicitation") return item.status === "pending";
  if (item.kind !== "tool") return false;
  const name = item.execution.name;
  if (name.endsWith("sys_user_inbox_post")) return true;
  return name.endsWith("sys_ticket_update") && addsNeedsHuman(item.execution.arguments);
}

/** Rows that can belong to a turn's body; anything else ends a background run. */
function isTurnBody(bubble: Bubble): boolean {
  return (
    bubble.kind === "assistant" ||
    bubble.kind === "subagent_activity" ||
    bubble.kind === "routing_decision"
  );
}

function payloadAddsNeedsHuman(payload: string): boolean {
  try {
    const parsed: unknown = JSON.parse(payload);
    const changes = (parsed as { changes?: Record<string, unknown> } | null)?.changes;
    return changes !== undefined && changes !== null && addsNeedsHuman(changes);
  } catch {
    return false;
  }
}

/** Whether a ticket-update input reports a ticket now marked ``needs-human``. */
function reportsNeedsHuman(bubble: Bubble): boolean {
  if (bubble.kind !== "user") return false;
  for (const match of userText(bubble).matchAll(TICKET_UPDATED_RE)) {
    if (payloadAddsNeedsHuman(match[1]!)) return true;
  }
  return false;
}

function hasText(bubble: Bubble): boolean {
  return (
    bubble.kind === "assistant" &&
    bubble.items.some((item) => item.kind === "text" && item.text.trim().length > 0)
  );
}

function turnAsksHuman(turn: Bubble[]): boolean {
  return (
    reportsNeedsHuman(turn[0]!) ||
    turn.some((bubble) => bubble.kind === "assistant" && bubble.items.some(itemAsksHuman))
  );
}

function ticketIdsOf(bubble: Bubble): string[] {
  if (bubble.kind !== "user") return [];
  return [...userText(bubble).matchAll(TICKET_ID_RE)].map((match) => (match[1] ?? match[2])!);
}

function backgroundRow(turns: Bubble[][]): BackgroundUpdatesBubble {
  const bubbles = turns.flat();
  const ticketIds = new Set<string>();
  for (const turn of turns) {
    for (const ticketId of ticketIdsOf(turn[0]!)) ticketIds.add(ticketId);
  }
  const first = turns[0]![0] as UserBubble;
  return {
    kind: "background_updates",
    itemId: first.itemId,
    bubbles,
    updateCount: turns.length,
    ticketCount: ticketIds.size,
    createdAtS: first.createdAtS,
  };
}

/** Whether the transcript belongs to a project orchestrator, which ticket updates reach. */
export function receivesTicketUpdates(bubbles: Bubble[]): boolean {
  return bubbles.some((bubble) => automaticInputKind(bubble) === "ticket_update");
}

/**
 * Replace each run of consecutive automatic turns with one background-updates row.
 *
 * Only a session that receives ticket updates (a project's orchestrator) folds,
 * so a worker's instructions from its coordinator stay in view.
 *
 * :param bubbles: Visible transcript rows, oldest first.
 * :returns: *bubbles* itself when nothing folds, else a new array.
 */
export function foldBackgroundTurns(bubbles: Bubble[]): Bubble[] {
  if (!receivesTicketUpdates(bubbles)) return bubbles;
  const out: Bubble[] = [];
  let run: Bubble[][] = [];
  let folded = false;
  const flushRun = () => {
    if (run.length > 0) {
      out.push(backgroundRow(run));
      folded = true;
    }
    run = [];
  };
  // A person's message not yet answered in text: inputs queued during that turn can
  // land before the reply, so the next automatic turn with text may hold the answer.
  let awaitingReply = false;
  let index = 0;
  while (index < bubbles.length) {
    const bubble = bubbles[index]!;
    if (automaticInputKind(bubble) === null) {
      flushRun();
      out.push(bubble);
      if (bubble.kind === "user") awaitingReply = true;
      else if (hasText(bubble)) awaitingReply = false;
      index += 1;
      continue;
    }
    let end = index + 1;
    while (end < bubbles.length && isTurnBody(bubbles[end]!)) end += 1;
    const turn = bubbles.slice(index, end);
    const answersPerson = awaitingReply && turn.some(hasText);
    if (answersPerson) awaitingReply = false;
    if (answersPerson || turnAsksHuman(turn)) {
      flushRun();
      out.push(...turn);
    } else {
      run.push(turn);
    }
    index = end;
  }
  flushRun();
  return folded ? out : bubbles;
}
