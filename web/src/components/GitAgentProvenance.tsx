import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { refreshAgent } from "@/lib/agentsApi";
import { ApiError } from "@/lib/sessionsApi";

/**
 * Render a git clone URL compactly as ``host/path`` without the trailing
 * ``.git`` (falling back to the raw string when it isn't a valid URL).
 */
export function gitUrlDisplay(url: string): string {
  try {
    const parsed = new URL(url);
    return parsed.host + parsed.pathname.replace(/\.git$/, "");
  } catch {
    return url;
  }
}

/**
 * Provenance line + Refresh action for a git-imported agent.
 *
 * Rendered inside the agent picker row when ``agent.git_url`` is set. The
 * Refresh button re-pulls the tracked branch's HEAD on the host that
 * imported the agent (``POST /v1/agents/{id}/refresh``) and invalidates the
 * ``["available-agents"]`` query so the new version/commit shows. A 409
 * (host offline) surfaces a distinct, actionable message.
 *
 * The button stops event propagation so clicking Refresh inside a
 * ``DropdownMenuItem`` doesn't also select the row / close the menu.
 */
export function GitAgentProvenance({ agent }: { agent: AvailableAgent }) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleRefresh(): Promise<void> {
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      await refreshAgent(agent.id);
      void queryClient.invalidateQueries({ queryKey: ["available-agents"] });
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError("Host offline — connect it to refresh.");
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setPending(false);
    }
  }

  const shortSha = agent.git_commit ? agent.git_commit.slice(0, 7) : null;
  const provenance = [
    gitUrlDisplay(agent.git_url ?? ""),
    agent.git_ref ? `@ ${agent.git_ref}` : null,
    shortSha ? `· ${shortSha}` : null,
    agent.version != null ? `· v${agent.version}` : null,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className="mt-0.5 flex flex-col gap-0.5" data-testid="git-agent-provenance">
      <span className="truncate text-[11px] text-muted-foreground/70">{provenance}</span>
      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={pending}
          aria-busy={pending}
          aria-label={`Refresh ${agent.display_name} from git`}
          data-testid={`git-agent-refresh-${agent.id}`}
          onClick={(e) => {
            e.stopPropagation();
            e.preventDefault();
            void handleRefresh();
          }}
          // Keep keydown from bubbling to the parent DropdownMenuItem, whose
          // roving-focus handler would otherwise treat Enter/Space as a row
          // select. Space/Enter here activate the button itself.
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.stopPropagation();
            }
          }}
          className="text-[11px] text-muted-foreground transition hover:text-foreground disabled:opacity-50"
        >
          {pending ? "Refreshing…" : "Refresh"}
        </button>
        {error && (
          <span role="alert" aria-live="assertive" className="text-[11px] text-destructive">
            {error}
          </span>
        )}
      </div>
    </div>
  );
}
