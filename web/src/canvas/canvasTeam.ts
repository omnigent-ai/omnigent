// A project's team on the Canvas: roles are session labels, and the team lead
// connects to every other card on its board.

import type { Edge } from "@xyflow/react";
import type { Conversation } from "@/hooks/useConversations";

export const TEAM_ROLE_LABEL_KEY = "omnigent.team_role";
export const TEAM_LEAD_ROLE = "Team lead";
export const TEAM_ROLE_PRESETS = [TEAM_LEAD_ROLE, "Frontend", "Backend"] as const;
export const MAX_TEAM_ROLE_LENGTH = 40;

export function teamRole(conversation: Conversation): string | null {
  return conversation.labels?.[TEAM_ROLE_LABEL_KEY]?.trim() || null;
}

export function isTeamLeadRole(role: string | null): boolean {
  return role?.trim().toLowerCase() === TEAM_LEAD_ROLE.toLowerCase();
}

/** Lines from the board's team lead to its other cards; none without a lead. */
export function teamEdges(sessions: readonly Conversation[]): Edge[] {
  const lead = sessions.find((session) => isTeamLeadRole(teamRole(session)));
  if (!lead) return [];
  return sessions
    .filter((session) => session.id !== lead.id)
    .map((session) => ({
      id: `team-${lead.id}-${session.id}`,
      source: lead.id,
      target: session.id,
      type: "team",
      selectable: false,
      focusable: false,
    }));
}

/** A new branch for a teammate's worktree, e.g. ``team/frontend-4f2a``. */
export function teamBranchName(role: string, suffix: string): string {
  const slug = role
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `team/${slug || "agent"}-${suffix}`;
}
