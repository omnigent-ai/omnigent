// A room's token usage and workspace files, read through Omnigent.

import { posix } from "node:path";
import type { OmnigentSessionListItem } from "./omnigentClient.ts";

export interface ProjectUsage {
  totalTokens: number;
  costUsd: number;
  /** Sessions whose usage was counted; archived sessions are not listed by Omnigent. */
  sessions: number;
}

export interface SessionUsage {
  tokens: number;
  costUsd: number;
}

/** Tokens and cost across every model a session used (`usage_by_model`). */
export function sessionUsage(detail: unknown): SessionUsage {
  const byModel = (detail as { usage_by_model?: unknown } | null)?.usage_by_model;
  let tokens = 0;
  let costUsd = 0;
  if (byModel && typeof byModel === "object") {
    for (const usage of Object.values(byModel as Record<string, Record<string, unknown>>)) {
      if (typeof usage?.total_tokens === "number") tokens += usage.total_tokens;
      if (typeof usage?.total_cost_usd === "number") costUsd += usage.total_cost_usd;
    }
  }
  return { tokens, costUsd };
}

/** Sums usage per project, re-reading a session only after it changes. */
export class UsageTracker {
  private readonly cache = new Map<string, { updatedAt: number; status?: string; usage: SessionUsage }>();
  private readonly getSession: (sessionId: string) => Promise<unknown>;

  constructor(getSession: (sessionId: string) => Promise<unknown>) {
    this.getSession = getSession;
  }

  async projectUsage(sessions: (Pick<OmnigentSessionListItem, "id" | "updated_at"> & { status?: string })[]): Promise<ProjectUsage> {
    const usages = await Promise.all(
      sessions.map(async (session) => {
        const cached = this.cache.get(session.id);
        // A running turn adds tokens before the session's update time moves, so re-read it.
        const running = session.status === "running" || session.status === "launching";
        if (cached && cached.updatedAt === session.updated_at && cached.status === session.status && !running) return cached.usage;
        try {
          const usage = sessionUsage(await this.getSession(session.id));
          this.cache.set(session.id, { updatedAt: session.updated_at, status: session.status, usage });
          return usage;
        } catch {
          // Not cached, so the next refresh tries this session again.
          return { tokens: 0, costUsd: 0 };
        }
      }),
    );
    return {
      totalTokens: usages.reduce((sum, usage) => sum + usage.tokens, 0),
      costUsd: usages.reduce((sum, usage) => sum + usage.costUsd, 0),
      sessions: sessions.length,
    };
  }
}

/**
 * A path inside the room's workspace, from one relative to it. Anything that
 * would leave the workspace is refused, so the office never browses elsewhere.
 */
export function workspacePath(workspace: string, relative: string): { absolute: string; relative: string } | null {
  const root = posix.normalize(workspace).replace(/(.)\/+$/, "$1");
  const cleaned = posix.normalize(relative.trim().replace(/^\/+/, "") || ".").replace(/\/+$/, "");
  if (cleaned === ".." || cleaned.startsWith("../")) return null;
  const inside = cleaned === "." ? "" : cleaned;
  return { absolute: inside ? posix.join(root, inside) : root, relative: inside };
}
