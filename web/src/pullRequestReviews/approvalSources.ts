// The list an approval was sent from, as it is now: the reader it reads as and each PR's own
// state and author. A send, and a Retry kept from it, goes only while that list is mounted and
// still reads as the approval was made, and judges the PR by what the list has now.

import { createLiveScopes } from "@/lib/liveScope";
import type { MacNamedReader } from "@/lib/macNamedReads";

export interface ApprovalSource {
  /** The page's own login the list reads as; null for the host's account. */
  reader: MacNamedReader | null;
  /** The PR's state and author as the list's own read of it has them; null until it's read. */
  facts: (url: string) => { open: boolean; author: string | null } | null;
}

export const APPROVAL_SOURCES = createLiveScopes<ApprovalSource>();
