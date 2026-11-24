// Parses the server's automatic ticket notifications (``ticket_batch_message`` in
// ``omnigent/server/project_orchestrator.py``) into per-ticket summaries so the chat
// can show them as cards. Display-only; anything unexpected returns null and the
// message renders as plain text.

import type { TicketLabel, TicketStage } from "./projectTicketsApi";

const HEADER_RE =
  /^(?:Automatic ticket update|Ticket update digest) for project ([0-9a-f]{32})\. [^\n]*$/;
const SECTION_RE =
  /^Ticket ([0-9a-f]{32}) · (created|updated|comment|execution|output)(?: · by (.+))?$/;
const DIGEST_HEADER_RE = /^Digest of \d+ background updates? /;
const DIGEST_LINE_RE = /^- ([0-9a-f]{32}) "(.*?)": (.+?)(?: — latest: (.*))?$/;
const ESCALATION_PREFIX = "No worker linked to this ticket replied";

export interface TicketUpdateComment {
  text: string;
  audience: "human" | "agents" | null;
  /** A person who acted, when the server names one (shared members, not the owner). */
  author: string | null;
  kind: "comment" | "execution";
  final: boolean;
}

export interface TicketUpdateCard {
  ticketId: string;
  /** Title from a digest line; full event sections carry none. */
  title: string | null;
  /** Stage the ticket moved to in these events, if any. */
  movedTo: TicketStage | null;
  /** The ticket's full label list after the latest label change, if any. */
  labels: TicketLabel[] | null;
  /** Other edited fields, e.g. "title" or "description". */
  editedFields: string[];
  created: boolean;
  outputs: string[];
  comment: TicketUpdateComment | null;
  /** Short change phrases from digest lines, e.g. "agent note ×2". */
  digestSummary: string | null;
  /** The latest note or output a digest line quotes. */
  digestLatest: string | null;
  /** A person's comment no worker answered in time. */
  escalated: boolean;
}

export interface TicketUpdateMessage {
  projectId: string;
  cards: TicketUpdateCard[];
}

function emptyCard(ticketId: string): TicketUpdateCard {
  return {
    ticketId,
    title: null,
    movedTo: null,
    labels: null,
    editedFields: [],
    created: false,
    outputs: [],
    comment: null,
    digestSummary: null,
    digestLatest: null,
    escalated: false,
  };
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function stageOf(payload: Record<string, unknown>): TicketStage | null {
  const stage = asRecord(payload.stage);
  if (!stage || typeof stage.id !== "string" || typeof stage.name !== "string") return null;
  return {
    id: stage.id,
    name: stage.name,
    instructions: "",
    is_complete: stage.is_complete === true,
    color: (stage.color as TicketStage["color"]) ?? null,
    icon: (stage.icon as TicketStage["icon"]) ?? null,
  };
}

function labelsOf(value: unknown): TicketLabel[] | null {
  if (!Array.isArray(value)) return null;
  const labels: TicketLabel[] = [];
  for (const entry of value) {
    const label = asRecord(entry);
    if (!label || typeof label.key !== "string") return null;
    labels.push({ key: label.key, value: typeof label.value === "string" ? label.value : null });
  }
  return labels;
}

/** Folds one event section into *card*; false when the payload isn't the expected shape. */
function applyEvent(
  card: TicketUpdateCard,
  kind: string,
  author: string | null,
  payload: Record<string, unknown>,
): boolean {
  if (kind === "created") {
    card.created = true;
    if (typeof payload.title === "string") card.title = payload.title;
    const stage = stageOf(payload);
    if (stage) card.movedTo = stage;
    const labels = labelsOf(payload.labels);
    if (labels?.length) card.labels = labels;
    return true;
  }
  if (kind === "comment" || kind === "execution") {
    if (typeof payload.text !== "string") return false;
    const audience =
      payload.audience === "human" || payload.audience === "agents" ? payload.audience : null;
    card.comment = { text: payload.text, audience, author, kind, final: payload.final === true };
    return true;
  }
  if (kind === "output") {
    if (typeof payload.url !== "string") return false;
    card.outputs.push(payload.url);
    return true;
  }
  // "updated": field edits, a stage move, or a session / related-ticket link.
  const changes = asRecord(payload.changes);
  if (changes) {
    const stage = stageOf(payload);
    if (stage) card.movedTo = stage;
    for (const [field, value] of Object.entries(changes)) {
      if (field === "stage_id" || field === "status") continue;
      if (field === "labels") {
        const labels = labelsOf(value);
        if (labels === null) return false;
        card.labels = labels;
      } else {
        const name = field.replaceAll("_", " ");
        if (!card.editedFields.includes(name)) card.editedFields.push(name);
      }
    }
    return true;
  }
  return "session_id" in payload || "related_ticket_id" in payload;
}

/**
 * Parse an automatic ticket-update turn into one card per ticket, in first-seen order.
 *
 * :param text: The turn's text exactly as the agent received it.
 * :returns: null when *text* isn't a ticket update or any part fails to parse.
 */
export function parseTicketUpdateMessage(text: string): TicketUpdateMessage | null {
  const lines = text.replace(/\r\n/g, "\n").trimEnd().split("\n");
  const header = HEADER_RE.exec(lines[0] ?? "");
  if (!header) return null;
  const cards = new Map<string, TicketUpdateCard>();
  const cardFor = (ticketId: string) => {
    let card = cards.get(ticketId);
    if (!card) {
      card = emptyCard(ticketId);
      cards.set(ticketId, card);
    }
    return card;
  };
  let current: TicketUpdateCard | null = null;
  let inDigest = false;
  for (let index = 1; index < lines.length; index += 1) {
    const line = lines[index]!;
    if (line.trim() === "") continue;
    const section = SECTION_RE.exec(line);
    if (section && !inDigest) {
      const payloadLine = lines[index + 1];
      if (payloadLine === undefined) return null;
      let payload: Record<string, unknown> | null;
      try {
        payload = asRecord(JSON.parse(payloadLine));
      } catch {
        return null;
      }
      if (!payload) return null;
      current = cardFor(section[1]!);
      if (!applyEvent(current, section[2]!, section[3] ?? null, payload)) return null;
      index += 1;
      continue;
    }
    if (DIGEST_HEADER_RE.test(line)) {
      inDigest = true;
      current = null;
      continue;
    }
    if (inDigest) {
      const digest = DIGEST_LINE_RE.exec(line);
      if (!digest) return null;
      const card = cardFor(digest[1]!);
      if (digest[2]) card.title = digest[2];
      card.digestSummary = digest[3]!;
      card.digestLatest = digest[4] ?? null;
      continue;
    }
    // Agent-facing notes after a section, e.g. the escalation or final-update reminder.
    if (current === null) return null;
    if (line.startsWith(ESCALATION_PREFIX)) current.escalated = true;
  }
  if (cards.size === 0) return null;
  return { projectId: header[1]!, cards: [...cards.values()] };
}
