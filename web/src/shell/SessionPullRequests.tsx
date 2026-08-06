/**
 * "Pull requests opened this session" list.
 *
 * Managed sandbox sessions can open several PRs (across several repos) as the
 * agent works. This surfaces them at the top of the Files panel: it polls the
 * session-scoped endpoint and renders each PR as a link. It self-gates — when
 * GitHub isn't connected, the session isn't managed, or no PRs have been opened
 * yet, it renders nothing, so it's safe to mount unconditionally.
 */

import { useQuery } from "@tanstack/react-query";
import { ExternalLinkIcon, GitPullRequestIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { type GithubSessionPull, fetchSessionPulls } from "@/lib/githubIntegration";
import { cn } from "@/lib/utils";

/** Derive a human status label + badge tint for a PR's current state. */
function prStatus(pr: GithubSessionPull): { label: string; className: string } {
  if (pr.merged) {
    return { label: "merged", className: "bg-purple-500/15 text-purple-300" };
  }
  if (pr.state === "closed") {
    return { label: "closed", className: "bg-red-500/15 text-red-300" };
  }
  if (pr.draft) {
    return { label: "draft", className: "bg-muted text-muted-foreground" };
  }
  return { label: "open", className: "bg-green-500/15 text-green-300" };
}

/** Props for {@link SessionPullRequests}. */
interface SessionPullRequestsProps {
  /** The session/conversation whose PRs to show, or undefined (renders nothing). */
  conversationId: string | undefined;
}

export function SessionPullRequests({ conversationId }: SessionPullRequestsProps) {
  const { data } = useQuery({
    queryKey: ["session-pulls", conversationId],
    queryFn: () => fetchSessionPulls(conversationId as string),
    enabled: !!conversationId,
    // Poll so PRs the agent opens mid-session appear without a manual refresh,
    // and refetch on focus. But each poll drives a GitHub *search* (30 req/min
    // authenticated, shared across tabs), so gate the cadence instead of
    // hammering every open session forever:
    //   - GitHub not connected → don't poll at all (nothing to find).
    //   - connected, PRs exist → poll 20s to keep their state fresh.
    //   - connected, none yet → back off to 60s (still catches the first PR,
    //     but a session that never opens one costs ~1 search/min, not 3).
    // GitHub's search index also lags a brand-new PR by a bit.
    refetchInterval: (query) => {
      const result = query.state.data;
      if (result && !result.connected) return false;
      return result && result.pulls.length > 0 ? 20_000 : 60_000;
    },
    refetchOnWindowFocus: true,
    staleTime: 10_000,
  });

  const pulls = data?.connected ? data.pulls : [];
  if (pulls.length === 0) {
    return null;
  }

  return (
    <div className="border-b border-border px-2 py-2" data-testid="session-pull-requests">
      <div className="flex items-center gap-1.5 px-1 pb-1.5 text-xs font-medium text-muted-foreground">
        <GitPullRequestIcon className="size-3.5 shrink-0" />
        Pull requests opened this session
        <span className="ml-1 rounded-full bg-muted px-1.5 text-[10px] leading-4 text-muted-foreground">
          {pulls.length}
        </span>
      </div>
      <ul className="flex flex-col gap-0.5">
        {pulls.map((pr) => {
          const status = prStatus(pr);
          return (
            <li key={`${pr.repo}#${pr.number}`}>
              <a
                href={pr.html_url ?? "#"}
                target="_blank"
                rel="noreferrer"
                className={cn(
                  "group flex items-center gap-1.5 rounded-md px-1.5 py-1 text-xs",
                  "text-foreground transition-colors hover:bg-muted",
                )}
                data-testid="session-pull-request"
              >
                <span className="shrink-0 text-muted-foreground">
                  {pr.repo}#{pr.number}
                </span>
                <span className="min-w-0 flex-1 truncate">{pr.title ?? pr.head_ref ?? ""}</span>
                <Badge
                  variant="secondary"
                  className={cn("shrink-0 text-[10px]", status.className)}
                  data-testid="session-pull-request-status"
                >
                  {status.label}
                </Badge>
                <ExternalLinkIcon className="size-3 shrink-0 opacity-0 transition-opacity group-hover:opacity-60" />
              </a>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
