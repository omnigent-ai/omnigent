// The PR detail's merge readiness: a header badge and a Summary section with the reason,
// plus the source's way to renew a credential when it needs one.

import {
  CircleCheckIcon,
  CircleDotIcon,
  CircleHelpIcon,
  CircleXIcon,
  GitMergeIcon,
  KeyRoundIcon,
  LoaderCircleIcon,
  type LucideIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  MERGE_READINESS_TONE_TEXT,
  presentMergeReadiness,
  type MergeReadinessTone,
  type MergeReadinessView,
} from "@/lib/mergeReadiness";
import { pullRequestIntegrations } from "@/lib/pullRequestIntegrations";
import { cn } from "@/lib/utils";

import { CheckSummaryHeadline } from "./PullRequestChecks";
import { usePullRequestSectionTitleClass } from "@/shell/pullRequestSectionStyle";

/** A status badge's border, fill and text for each tint, in the theme's status tokens. */
export const MERGE_READINESS_BADGE_TONES: Record<MergeReadinessTone, string> = {
  green: "border-status-green/25 bg-status-green/10 text-status-green",
  red: "border-status-red/25 bg-status-red/10 text-status-red",
  yellow: "border-status-yellow/25 bg-status-yellow/10 text-status-yellow",
  purple: "border-status-purple/25 bg-status-purple/10 text-status-purple",
  gray: "border-border bg-muted text-muted-foreground",
};

const TONE_ICONS: Record<MergeReadinessTone, LucideIcon> = {
  green: CircleCheckIcon,
  red: CircleXIcon,
  yellow: CircleDotIcon,
  purple: GitMergeIcon,
  gray: CircleHelpIcon,
};

/** Whether the header shows a merge status badge for this view. */
export function hasMergeReadinessBadge(view: MergeReadinessView | undefined): boolean {
  return presentMergeReadiness(view) !== null;
}

/** The header's merge status badge for an open PR; nothing where GitHub-only status shows. */
export function MergeReadinessBadge({ view }: { view: MergeReadinessView | undefined }) {
  const presentation = presentMergeReadiness(view);
  if (!presentation) return null;
  return (
    <Badge
      aria-label={`Merge status: ${presentation.label}`}
      title={presentation.detail}
      className={cn(
        "h-5 shrink-0 rounded-full border px-2 py-px text-xs leading-none",
        MERGE_READINESS_BADGE_TONES[presentation.tone],
      )}
    >
      {presentation.short}
    </Badge>
  );
}

/** The Summary tab's merge status: what the source and GitHub say, and why. */
export function PullRequestMergeReadinessSection({
  view,
  onRenewed,
}: {
  view: MergeReadinessView | undefined;
  /** Runs once the source's credential is valid again, to re-read the merge status. */
  onRenewed: () => void;
}) {
  const titleClass = usePullRequestSectionTitleClass();
  const presentation = presentMergeReadiness(view);
  if (!view || !presentation) return null;
  const credential = view.kind === "unknown" && view.failure === "credential";
  const CredentialRenewal = pullRequestIntegrations().mergeReadiness?.CredentialRenewal;
  const Icon =
    view.kind === "checking"
      ? LoaderCircleIcon
      : credential
        ? KeyRoundIcon
        : TONE_ICONS[presentation.tone];
  return (
    <section className="space-y-2" aria-labelledby="pull-request-merge-status-heading">
      <h3 id="pull-request-merge-status-heading" className={titleClass}>
        Merge status
      </h3>
      <CheckSummaryHeadline
        Icon={Icon}
        iconClass={cn(
          MERGE_READINESS_TONE_TEXT[presentation.tone],
          view.kind === "checking" && "animate-spin",
        )}
        title={presentation.label}
        detail={presentation.detail}
      />
      {credential && CredentialRenewal && <CredentialRenewal onRenewed={onRenewed} />}
    </section>
  );
}
