// What each kind of stack may be used for. A description read exactly may be rewritten, a
// loose read only navigated, and GitHub's own stack navigated and compared but never edited,
// and merged only on GitHub.

import type { StackParse, VerifiedStackParse } from "./stackModel";

/** Previous, Next, the stack menu and card: every stack can be moved through. */
export function navigableStack(parse: StackParse): boolean {
  switch (parse.trust) {
    case "verified":
    case "tolerated":
    case "native":
      return true;
  }
}

/** Counting a PR's own changes from its parent's head needs the parent to be certain. */
export function trustedForComparison(parse: StackParse): boolean {
  switch (parse.trust) {
    case "verified":
    case "native":
      return true;
    case "tolerated":
      return false;
  }
}

/** Only metadata matched exactly in the description may be stripped, spliced or edited. */
export function descriptionMetadataEditable(parse: StackParse): parse is VerifiedStackParse {
  switch (parse.trust) {
    case "verified":
      return true;
    case "tolerated":
    case "native":
      return false;
  }
}

/** GitHub merges its own stack together, and only on GitHub; a description's PRs merge one by one. */
export function mergesOnGitHub(parse: StackParse): boolean {
  switch (parse.trust) {
    case "native":
      return true;
    case "verified":
    case "tolerated":
      return false;
  }
}
