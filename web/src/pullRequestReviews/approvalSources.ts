// The list an approval was sent from, as it is now: each PR's own state and author. A send,
// and a Retry kept from it, goes only while that list is mounted, and judges the PR by what
// the list has now.

import { createLiveScopes } from "@/lib/liveScope";

export interface ApprovalSource {
  /** The PR's state and author as the list's own read of it has them; null until it's read. */
  facts: (url: string) => { open: boolean; author: string | null } | null;
}

export const APPROVAL_SOURCES = createLiveScopes<ApprovalSource>();
