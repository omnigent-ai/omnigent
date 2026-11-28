// What a pull request's comment box says when the session's host can't post: why, in words the
// user can act on, and where to comment instead. The draft stays editable.

import type { GitProviderCopy } from "@/lib/gitProviders";

/** Why the box can't post, as the diagnostics log names it. */
export type CommentWritesReason = "host_cannot_comment";

/** The provider as the panel names it, from its shared copy. */
export type CommentWritesProvider = Pick<GitProviderCopy, "id" | "label" | "requestName">;

export interface CommentWritesRecovery {
  reason: CommentWritesReason;
  message: string;
  /** Posting waits on this. */
  blocksPosting: boolean;
  openLabel: string;
}

/** "Open on GitLab", from the provider's own name; a provider the panel can't name opens in the browser. */
function openLabel(provider: CommentWritesProvider): string {
  return provider.id ? `Open on ${provider.label}` : "Open in Browser";
}

/** The box's recovery while the session's host can't post, or null when it can. */
export function commentWritesRecovery(
  provider: CommentWritesProvider | null,
): CommentWritesRecovery | null {
  if (!provider) return null;
  return {
    reason: "host_cannot_comment",
    message: `The session’s machine can’t post comments on this ${provider.requestName}.`,
    blocksPosting: true,
    openLabel: openLabel(provider),
  };
}
