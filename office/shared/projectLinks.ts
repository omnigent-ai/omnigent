// Links to a project's results: the app, a deployment, a pull request, a report.

import type { AgentRoleId } from "./sdlcTheme.ts";

export interface ProjectLink {
  id: string;
  label: string;
  url: string;
  description: string | null;
  /** The PO, or the role that published it. */
  addedBy: "po" | AgentRoleId;
  at: number;
}

export const MAX_LINKS_PER_PROJECT = 20;

/** A link's fields from untrusted input, or why they are not acceptable. */
export function parseLinkInput(input: unknown): { label: string; url: string; description: string | null } | { error: string } {
  const record = (input && typeof input === "object" ? input : {}) as Record<string, unknown>;
  const label = typeof record.label === "string" ? record.label.trim() : "";
  const raw = typeof record.url === "string" ? record.url.trim() : "";
  const description = typeof record.description === "string" && record.description.trim() ? record.description.trim().slice(0, 300) : null;
  if (!label || label.length > 80) return { error: "label is required (at most 80 characters)" };
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return { error: "url must be a full http(s) address" };
  }
  // Only web links: no javascript:, file:, or data: URLs in the PO's view.
  if (url.protocol !== "http:" && url.protocol !== "https:") return { error: "url must start with http:// or https://" };
  if (url.href.length > 2000) return { error: "url is too long" };
  return { label, url: url.href, description };
}
