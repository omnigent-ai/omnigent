// The new pull request page and the classic view read the same session's pull requests, so
// their cache keys must never meet: neither view may read or overwrite the other's entries.

import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import {
  pullRequestInfoQueryKey,
  pullRequestLabelsMutationKey,
  pullRequestReviewersMutationKey,
} from "@/hooks/usePullRequests";

import { PULL_REQUEST_READ_FAMILIES } from "./pullRequestRecord";

const URL = "https://github.com/acme/app/pull/7";

describe("the new page's and the classic view's cache keys", () => {
  it("share no family", () => {
    const classic = [
      pullRequestInfoQueryKey("conv", URL)[0],
      pullRequestLabelsMutationKey("conv")[0],
      pullRequestReviewersMutationKey("conv")[0],
    ];
    for (const family of PULL_REQUEST_READ_FAMILIES) {
      expect(family).toMatch(/^pr-page-github-/);
      expect(classic).not.toContain(family);
    }
  });

  it("never share an entry for the same session and URL", () => {
    const client = new QueryClient();
    const pageKey = ["pr-page-github-info", "conv", URL];
    client.setQueryData(pullRequestInfoQueryKey("conv", URL), { from: "classic" });
    client.setQueryData(pageKey, { from: "page" });

    // Each view's session-wide refresh drops only its own entries.
    client.removeQueries({ queryKey: ["pr-page-github-info", "conv"] });
    expect(client.getQueryData(pullRequestInfoQueryKey("conv", URL))).toEqual({ from: "classic" });
    client.setQueryData(pageKey, { from: "page" });
    client.removeQueries({ queryKey: pullRequestInfoQueryKey("conv") });
    expect(client.getQueryData(pageKey)).toEqual({ from: "page" });
  });
});
