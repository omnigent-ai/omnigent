/**
 * Pure view-model derivation for Sessions tree items.
 *
 * No VS Code imports: produces a plain `SessionItemView` (theme-icon *id*,
 * context value, tooltip text) that the thin `SessionsTreeProvider` maps onto
 * `vscode.TreeItem`. Unit-testable in isolation.
 */
import type { Session } from "../api/client";

export interface SessionItemView {
  id: string;
  label: string;
  description: string;
  tooltip: string;
  themeIconId: string;
  contextValue: string;
}

/** A readable label: the title when present, else a short fallback from the id. */
export function deriveLabel(s: Session): string {
  if (s.title && s.title.trim() !== "") return s.title.trim();
  const id = s.id ?? "";
  // Strip a "conv_" style prefix and keep a short, readable tail.
  const tail = id.includes("_") ? id.slice(id.indexOf("_") + 1) : id;
  const short = tail.slice(0, 8);
  return short ? `Session ${short}` : "Session";
}

/** Render a unix-SECONDS timestamp as a coarse relative time ("just now", "3m ago"…). */
export function relativeTime(unixSecs: number, nowMs: number): string {
  const thenMs = unixSecs * 1000;
  const diffSec = Math.max(0, Math.round((nowMs - thenMs) / 1000));
  if (diffSec < 60) return "just now";
  const min = Math.floor(diffSec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}

/** Map a session status/archived flag to a VS Code ThemeIcon id. */
export function statusThemeIconId(status?: string, archived?: boolean): string {
  if (archived === true) return "archive";
  const s = (status ?? "").toLowerCase();
  if (s === "running") return "play-circle";
  if (s === "idle") return "circle-outline";
  if (s.includes("error") || s.includes("fail")) return "error";
  return "circle-outline";
}

/** Build the full item view-model for a session at the given wall-clock time. */
export function toItemView(s: Session, nowMs: number): SessionItemView {
  const label = deriveLabel(s);
  const parts: string[] = [];
  if (s.agent_name) parts.push(s.agent_name);
  if (typeof s.updated_at === "number") parts.push(relativeTime(s.updated_at, nowMs));
  const description = parts.join(" · ");

  const tipLines: string[] = [];
  if (s.workspace) tipLines.push(`Workspace: ${s.workspace}`);
  if (s.git_branch) tipLines.push(`Branch: ${s.git_branch}`);
  if (s.status) tipLines.push(`Status: ${s.status}`);
  if (typeof s.created_at === "number") {
    tipLines.push(`Created: ${relativeTime(s.created_at, nowMs)}`);
  }
  if (typeof s.updated_at === "number") {
    tipLines.push(`Updated: ${relativeTime(s.updated_at, nowMs)}`);
  }
  const tooltip = tipLines.length > 0 ? `${label}\n\n${tipLines.join("\n")}` : label;

  return {
    id: s.id,
    label,
    description,
    tooltip,
    themeIconId: statusThemeIconId(s.status, s.archived),
    contextValue: "omnigentSession",
  };
}

/** Copy and sort by `updated_at` descending, with id as a stable tiebreak. */
export function sortSessions(list: Session[]): Session[] {
  return [...list].sort((a, b) => {
    const au = a.updated_at ?? 0;
    const bu = b.updated_at ?? 0;
    if (bu !== au) return bu - au;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
}
