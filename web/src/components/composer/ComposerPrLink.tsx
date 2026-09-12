import GithubMono from "@lobehub/icons/es/Github/components/Mono";
import { COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS } from "@/components/composer/ChatComposer";
import { cn } from "@/lib/utils";

/**
 * PR chip for the composer workspace bar: the session's associated pull
 * request(s) as a GitHub link that opens the workspace rail's GitHub tab.
 * Shows ``#123`` for one PR or ``N PRs`` for several. Self-nulls when there is
 * no PR or no way to open the tab (e.g. the landing window).
 *
 * @param prCount - Number of PRs associated with the session.
 * @param prNumber - The primary PR's number, shown when ``prCount === 1``.
 * @param onOpen - Opens the GitHub tab; ``null`` hides the link.
 */
export function ComposerPrLink({
  prCount,
  prNumber,
  onOpen,
  className,
}: {
  prCount: number;
  prNumber: number | null;
  onOpen: (() => void) | null;
  className?: string;
}) {
  if (prCount <= 0 || !onOpen) return null;

  return (
    <button
      type="button"
      data-testid="composer-pr-link"
      onClick={() => onOpen()}
      title={prCount > 1 ? "View these PRs in the GitHub tab" : "View this PR in the GitHub tab"}
      className={cn(
        "flex shrink-0 items-center gap-1.5 rounded text-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
        className,
      )}
    >
      <GithubMono size={14} aria-hidden />
      <span
        className={cn(
          "tabular-nums whitespace-nowrap underline underline-offset-2",
          COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS,
        )}
      >
        {prCount > 1 ? `${prCount} PRs` : `#${prNumber}`}
      </span>
    </button>
  );
}
