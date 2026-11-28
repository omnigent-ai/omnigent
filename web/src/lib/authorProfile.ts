// A pull request author's profile, as an older desktop app's read names it: GitHub's URL for
// the account and its kind. Nothing reads it any more; only the PR details' wire type in
// usePullRequests still names it, and goes with it once that file is free to change.

export type AuthorKind = "User" | "Bot" | "Mannequin" | "EnterpriseUserAccount" | "Organization";

export interface AuthorProfile {
  url: string;
  /** Null where GitHub named a kind this page doesn't know. */
  kind: AuthorKind | null;
}
