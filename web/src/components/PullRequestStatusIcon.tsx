import { pullRequestStatusPresentation, type PullRequestStatus } from "@/lib/pullRequestStatus";
import { cn } from "@/lib/utils";

export function PullRequestStatusIcon({
  status,
  colored = true,
  className,
}: {
  status: PullRequestStatus;
  colored?: boolean;
  className?: string;
}) {
  const { Icon, className: statusClassName } = pullRequestStatusPresentation(status);
  return (
    <Icon aria-hidden className={cn("size-3.5 shrink-0", colored && statusClassName, className)} />
  );
}
