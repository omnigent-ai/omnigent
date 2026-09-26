import { GitPullRequestIcon, Loader2Icon } from "lucide-react";
import { GitlabIcon } from "@/components/icons/GitlabIcon";
import { cn } from "@/lib/utils";

export type ComposerPrState = "loading" | "ready" | "unknown";

/**
 * Code-review chip for the composer workspace bar. Shows GitHub PRs or GitLab
 * MRs and opens the matching workspace rail tab. Multiple provider chips can be
 * rendered together when one branch is published to several upstreams.
 */
export function ComposerPrLink({
  state,
  prCount,
  prNumber,
  onOpen,
  provider = "github",
  className,
}: {
  state: ComposerPrState;
  prCount: number;
  prNumber: number | null;
  onOpen: (() => void) | null;
  provider?: "github" | "gitlab";
  className?: string;
}) {
  const isGitlab = provider === "gitlab";
  const singular = isGitlab ? "MR" : "PR";
  const plural = isGitlab ? "MRs" : "PRs";
  const providerName = isGitlab ? "GitLab" : "GitHub";
  const ProviderIcon = isGitlab ? GitlabIcon : null;
  if (state === "loading") {
    return (
      <span
        data-testid="composer-pr-loading"
        className={cn("flex shrink-0 items-center gap-1 text-sm text-muted-foreground", className)}
      >
        <Loader2Icon className="size-3.5 animate-spin" aria-hidden />
        <span>Checking {singular}…</span>
      </span>
    );
  }
  if (state === "unknown") {
    return (
      <span
        data-testid="composer-pr-unknown"
        className={cn("flex shrink-0 items-center gap-1 text-sm text-muted-foreground", className)}
      >
        {ProviderIcon ? (
          <ProviderIcon className="size-3.5 shrink-0" aria-hidden />
        ) : (
          <GitPullRequestIcon className="size-3.5 shrink-0" aria-hidden />
        )}
        <span>{singular} unavailable</span>
      </span>
    );
  }
  if (prCount <= 0 || !onOpen) return null;

  const label =
    prCount > 1
      ? `${prCount} ${plural}`
      : prNumber == null
        ? `1 ${singular}`
        : `${isGitlab ? "!" : "#"}${prNumber}`;

  return (
    <button
      type="button"
      data-testid={isGitlab ? "composer-gitlab-review-link" : "composer-pr-link"}
      onClick={() => onOpen()}
      aria-label={label}
      title={
        prCount > 1
          ? `View these ${plural} in the ${providerName} tab`
          : `View this ${singular} in the ${providerName} tab`
      }
      className={cn(
        "group flex min-w-0 items-center gap-1 rounded text-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
        className,
      )}
    >
      {ProviderIcon ? (
        <ProviderIcon className="size-3.5 shrink-0" aria-hidden />
      ) : (
        <GitPullRequestIcon className="size-3.5 shrink-0" aria-hidden />
      )}
      {/* Short and informative, so it stays when the bar collapses; a PR
          number that would truncate still asks the bar to collapse the
          directory and branch text, which frees the room it needs. */}
      <span
        data-workspace-collapse-label=""
        className="truncate tabular-nums underline-offset-2 group-hover:underline group-focus-visible:underline"
        title={label}
      >
        {label}
      </span>
    </button>
  );
}
