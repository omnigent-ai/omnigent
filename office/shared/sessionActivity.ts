// Visible session activity from Omnigent conversation items, for the browser
// and the service alike.

/** The parts of an Omnigent conversation item the office reads. */
export interface ActivityItem {
  id: string;
  type: string;
  role?: string;
  created_at?: number;
  content?: { type: string; text?: string }[];
  name?: string;
  message?: string;
  is_meta?: boolean;
  office_relayed?: boolean;
}

export interface ActivityEntry {
  id: string;
  kind: "you" | "agent" | "office" | "tool" | "error";
  text: string;
  at?: number;
}

const MAX_ENTRY_LENGTH = 400;

function clip(text: string): string {
  return text.length > MAX_ENTRY_LENGTH ? `${text.slice(0, MAX_ENTRY_LENGTH)}…` : text;
}

function messageText(item: ActivityItem): string {
  return (item.content ?? [])
    .map((block) => block.text ?? "")
    .join("")
    .trim();
}

/**
 * The agent messages answering any of `sent` (texts the PO sent this session), from
 * that message until the next user turn. `items` are oldest first.
 */
export function repliesTo(sent: Set<string>, items: ActivityItem[]): ActivityEntry[] {
  const replies: ActivityEntry[] = [];
  let answering = false;
  for (const item of items) {
    if (item.type !== "message" || item.is_meta) continue;
    const text = messageText(item);
    if (item.role === "user") answering = !item.office_relayed && sent.has(text);
    else if (answering && text) replies.push({ id: item.id, kind: "agent", text, at: item.created_at });
  }
  return replies;
}

/**
 * Visible activity only: messages, tool calls, and errors. Reasoning items
 * are deliberately skipped so the office never implies access to them.
 */
export function toActivity(items: ActivityItem[]): ActivityEntry[] {
  const entries: ActivityEntry[] = [];
  for (const item of items) {
    if (item.type === "message" && !item.is_meta) {
      const text = messageText(item);
      const kind = item.role !== "user" ? "agent" : item.office_relayed ? "office" : "you";
      if (text) entries.push({ id: item.id, kind, text: clip(text), at: item.created_at });
    } else if (item.type === "function_call" && item.name) {
      entries.push({ id: item.id, kind: "tool", text: `Used tool ${item.name}`, at: item.created_at });
    } else if (item.type === "error" && item.message) {
      entries.push({ id: item.id, kind: "error", text: clip(item.message), at: item.created_at });
    }
  }
  return entries;
}
