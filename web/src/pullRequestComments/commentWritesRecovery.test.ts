import { describe, expect, it } from "vitest";

import { gitProviderCopy } from "@/lib/gitProviders";
import { commentWritesRecovery } from "./commentWritesRecovery";

describe("commentWritesRecovery", () => {
  it("has nothing to say when the session's host can post", () => {
    expect(commentWritesRecovery(null)).toBeNull();
  });

  it("explains that the session's host can't post, and blocks posting", () => {
    expect(commentWritesRecovery(gitProviderCopy("github"))).toMatchObject({
      reason: "host_cannot_comment",
      message: "The session’s machine can’t post comments on this pull request.",
      blocksPosting: true,
      openLabel: "Open on GitHub",
    });
  });

  it("names a GitLab merge request's own provider, and opens an unnamed one in the browser", () => {
    const gitlab = gitProviderCopy("gitlab", null, {
      id: "gitlab",
      display_name: "GitLab",
      request_name: "merge request",
      number_prefix: "!",
    });
    expect(commentWritesRecovery(gitlab)).toMatchObject({
      openLabel: "Open on GitLab",
      message: "The session’s machine can’t post comments on this merge request.",
    });
    expect(commentWritesRecovery(gitProviderCopy(null))!.openLabel).toBe("Open in Browser");
  });
});
