import { CircleCheckIcon, CircleDotIcon, CircleXIcon, GitCompareArrowsIcon } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { TicketPullRequestStatus } from "@/lib/projectTicketsApi";
import { pullRequestStatusPresentation } from "@/lib/pullRequestStatus";
import { cn } from "@/lib/utils";

interface Pill {
  label: string;
  className: string;
  Icon: LucideIcon | null;
}

const RED = "text-red-700 dark:text-red-400";
const GREEN = "text-green-700 dark:text-green-400";
const AMBER = "text-amber-700 dark:text-amber-400";

// Only states that need a look carry colour; Merged keeps GitHub's purple.
const NEUTRAL = "text-muted-foreground";

/** The PR's lifecycle, without an icon: the row's PR icon already says what it is. */
function statePill(status: TicketPullRequestStatus): Pill {
  const key =
    status.state === "MERGED"
      ? "merged"
      : status.state === "CLOSED"
        ? "closed"
        : status.is_draft
          ? "draft"
          : "open";
  const { label, className } = pullRequestStatusPresentation(key);
  return { label, className: key === "merged" ? className : NEUTRAL, Icon: null };
}

/** CI for an open PR: failing beats running beats passing; none when it ran no checks. */
function checksPill(status: TicketPullRequestStatus): Pill | null {
  if (status.state !== "OPEN" || status.checks_supported === false) return null;
  const { failing, pending, total } = status.checks;
  if (failing > 0)
    return {
      label: `${failing} ${failing === 1 ? "check" : "checks"} failing`,
      className: RED,
      Icon: CircleXIcon,
    };
  if (pending > 0) return { label: "Checks running", className: AMBER, Icon: CircleDotIcon };
  if (total > 0) return { label: "Checks passing", className: GREEN, Icon: CircleCheckIcon };
  return null;
}

/** The PR's state, plus its merge conflicts and CI when it's open. */
export function ticketPullRequestPills(status: TicketPullRequestStatus): Pill[] {
  const pills = [statePill(status)];
  if (status.state === "OPEN" && status.merge_state_status?.toUpperCase() === "DIRTY")
    pills.push({ label: "Merge conflicts", className: RED, Icon: GitCompareArrowsIcon });
  const checks = checksPill(status);
  if (checks) pills.push(checks);
  return pills;
}

export function TicketPullRequestPills({ status }: { status: TicketPullRequestStatus }) {
  return (
    <span data-testid="pr-status-pills" className="flex min-w-0 flex-wrap items-center gap-1.5">
      {ticketPullRequestPills(status).map(({ label, className, Icon }) => (
        <span
          key={label}
          className={cn(
            "inline-flex h-5 items-center gap-1 rounded-full bg-current/10 px-2 text-xs font-medium whitespace-nowrap",
            className,
          )}
        >
          {Icon && <Icon aria-hidden className="size-3 shrink-0" />}
          {label}
        </span>
      ))}
    </span>
  );
}
