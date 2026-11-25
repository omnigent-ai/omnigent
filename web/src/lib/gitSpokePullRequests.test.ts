import { describe, expect, it } from "vitest";

import {
  GITSPOKE_WRITE_REASON,
  gitSpokeWriteReason,
  isGitSpokePullRequest,
} from "./gitSpokePullRequests";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

const SPOKE = "https://2.spoke.example.com/example-org/universe/pull/7";
const GITHUB = "https://github.com/acme/app/pull/8";

describe("GitSpoke pull requests are read-only on this Mac", () => {
  it("names GitSpoke as where to change a spoke PR, and nothing else", () => {
    expect(isGitSpokePullRequest(SPOKE)).toBe(true);
    expect(gitSpokeWriteReason(SPOKE)).toBe(GITSPOKE_WRITE_REASON);
    for (const url of [GITHUB, "https://ghe.example.com/a/b/pull/1", "not a url"]) {
      expect(isGitSpokePullRequest(url), url).toBe(false);
      expect(gitSpokeWriteReason(url), url).toBeNull();
    }
  });
});
