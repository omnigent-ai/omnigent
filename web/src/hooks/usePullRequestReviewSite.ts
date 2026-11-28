// The registered review site, where its links show here.

import { pullRequestIntegrations, type PullRequestReviewSite } from "@/lib/pullRequestIntegrations";

function useNoReviewSite(): boolean {
  return false;
}

/** The review site whose links show here, or null where none is registered or it's off. */
export function useEnabledReviewSite(): PullRequestReviewSite | null {
  const site = pullRequestIntegrations().reviewSite;
  const useEnabled = site?.useEnabled ?? useNoReviewSite;
  const enabled = useEnabled();
  return site && enabled ? site : null;
}
