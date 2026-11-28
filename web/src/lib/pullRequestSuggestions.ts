// Pull requests to suggest when linking one to a session, read through the
// GitHub fast track as the session host's account.

import { queryGitHubFastTrack } from "@/lib/githubFastTrack";
import type { GitHubFastTrackAccount, GitHubFastTrackError } from "@/lib/nativeBridge";

export const PULL_REQUEST_SUGGESTIONS_OPERATION = "pull_request_suggestions";

export interface PullRequestSuggestion {
  url: string;
  number: number;
  title: string;
  /** OPEN, CLOSED, or MERGED. */
  state: string | null;
  is_draft: boolean;
  /** `owner/repo`. */
  repository: string | null;
  updated_at: string | null;
}

export interface PullRequestSuggestions {
  /** Open PRs in the session's repository, or null when it has none. */
  repository: PullRequestSuggestion[] | null;
  /** The account's own open PRs, in any repository. */
  mine: PullRequestSuggestion[];
  /** Recently updated PRs the account is involved in, including closed ones. */
  recent: PullRequestSuggestion[];
}

/** A failed suggestions read, carrying the fast track's user-facing message. */
export class PullRequestSuggestionsError extends Error {
  readonly code: GitHubFastTrackError["code"];
  constructor(error: GitHubFastTrackError) {
    super(error.message);
    this.code = error.code;
  }
}

/** Read suggestions for `repository` (`owner/repo`, when the session has one) as `account`. */
export async function fetchPullRequestSuggestions(
  account: GitHubFastTrackAccount,
  repository: string | null,
): Promise<PullRequestSuggestions> {
  const result = await queryGitHubFastTrack<PullRequestSuggestions>(
    PULL_REQUEST_SUGGESTIONS_OPERATION,
    account,
    repository ? { repository } : {},
  );
  if (!result.ok) throw new PullRequestSuggestionsError(result.error);
  return result.data;
}
