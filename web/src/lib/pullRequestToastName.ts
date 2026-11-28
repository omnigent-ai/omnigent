// How a toast names the pull request an action changed: its number alone, as "#123". The
// title says too much for a line that only confirms what happened.

import { githubPullRequestLocation } from "@/lib/pullRequestReference";

/** "#123" for a PR given by its number or its link; a link that names none reads generically. */
export function pullRequestToastName(pullRequest: { number: number } | string): string {
  const number =
    typeof pullRequest === "string"
      ? githubPullRequestLocation(pullRequest)?.number
      : pullRequest.number;
  return number === undefined ? "The pull request" : `#${number}`;
}
