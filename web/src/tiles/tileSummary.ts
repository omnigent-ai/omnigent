// Text for a tile's header and details, derived from its session row.

import type { Conversation } from "@/hooks/useConversations";
import type { SessionState } from "@/hooks/useSessionState";
import type { ModelUsage } from "@/lib/types";

export function projectName(workspace: string | null | undefined): string | null {
  const trimmed = workspace?.trim().replace(/\/+$/, "");
  if (!trimmed) return null;
  return trimmed.slice(trimmed.lastIndexOf("/") + 1) || trimmed;
}

export function relativeAge(updatedAtSeconds: number, nowMs = Date.now()): string {
  const minutes = Math.max(0, Math.round((nowMs - updatedAtSeconds * 1000) / 60000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function statusLabel(state: SessionState | null, status: Conversation["status"]): string {
  switch (state?.kind) {
    case "awaiting":
      return "Needs your response";
    case "running":
    case "starting":
      return "Running";
    default:
      return status === "failed" ? "Failed" : "Idle";
  }
}

/** What the agent is doing beyond its status, e.g. "2 questions waiting". */
export function activityNotes(conversation: Conversation): string[] {
  const notes: string[] = [];
  const questions = conversation.pending_elicitations_count ?? 0;
  if (questions > 0) notes.push(`${questions} question${questions === 1 ? "" : "s"} waiting`);
  if (conversation.blocked_on) notes.push(`Blocked on ${conversation.blocked_on}`);
  if (conversation.background_work_running) notes.push("Background work running");
  return notes;
}

/** Token totals across models; null when no model reported any. */
export function usageTotals(
  usageByModel: Record<string, ModelUsage> | null,
): { input: number; output: number; total: number } | null {
  const models = Object.values(usageByModel ?? {});
  if (models.length === 0) return null;
  const input = models.reduce((sum, usage) => sum + (usage.inputTokens ?? 0), 0);
  const output = models.reduce((sum, usage) => sum + (usage.outputTokens ?? 0), 0);
  const total = models.reduce(
    (sum, usage) =>
      sum + (usage.totalTokens ?? (usage.inputTokens ?? 0) + (usage.outputTokens ?? 0)),
    0,
  );
  return { input, output, total };
}
