// Which keys and filters reach which session's PR reads, with a real QueryClient.

import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import {
  invalidatePullRequestEverywhere,
  invalidateSessionPullRequestReads,
  pullRequestReadRestAt,
  pullRequestRecordFilter,
  pullRequestRecordKey,
  pullRequestRecordRead,
  sessionRecordOwner,
} from "./pullRequestRecord";

const PR = "https://github.com/acme/app/pull/7";
const OTHER_PR = "https://github.com/acme/app/pull/8";

describe("record keys", () => {
  it("name the session, the PR and the family's own parts", () => {
    const owner = sessionRecordOwner("conv_1");
    const key = pullRequestRecordKey("pr-page-github-changed-files", PR, owner, "b:h");
    expect(key).toEqual(["pr-page-github-changed-files", "conv_1", PR, "b:h"]);
    expect(key[pullRequestReadRestAt(key)]).toBe("b:h");
    expect(pullRequestRecordRead("pr-page-github-changed-files", "conv_1", PR, "b:h")).toEqual({
      owner,
      queryKey: key,
    });
  });
});

describe("pullRequestRecordFilter", () => {
  it("reaches only its own session's entries of its family, every revision of them", () => {
    const client = new QueryClient();
    const owners = { a: sessionRecordOwner("conv_a"), b: sessionRecordOwner("conv_b") };
    for (const [name, owner] of Object.entries(owners)) {
      for (const family of ["pr-page-github-pr-diff", "pr-page-github-changed-files"] as const) {
        for (const revision of ["b:h1", "b:h2"]) {
          client.setQueryData(pullRequestRecordKey(family, PR, owner, revision), { name });
        }
      }
      client.setQueryData(pullRequestRecordKey("pr-page-github-pr-diff", OTHER_PR, owner, "b:h1"), {
        name,
      });
    }
    for (const [name, owner] of Object.entries(owners)) {
      const reached = client
        .getQueryCache()
        .findAll(pullRequestRecordFilter("pr-page-github-pr-diff", PR, owner))
        .map((query) => query.queryKey);
      expect(reached).toHaveLength(2);
      for (const key of reached) expect(client.getQueryData(key)).toEqual({ name });
    }
  });

  it("refreshes every session's entries of a PR, and nothing of another PR", async () => {
    const client = new QueryClient();
    const keys = [
      pullRequestRecordKey("pr-page-github-info", PR, sessionRecordOwner("conv_a")),
      pullRequestRecordKey(
        "pr-page-github-review-threads",
        PR,
        sessionRecordOwner("conv_b"),
        "b:h",
      ),
    ];
    const other = pullRequestRecordKey(
      "pr-page-github-info",
      OTHER_PR,
      sessionRecordOwner("conv_a"),
    );
    for (const key of [...keys, other]) client.setQueryData(key, {});
    await invalidatePullRequestEverywhere(client, PR);
    for (const key of keys) expect(client.getQueryState(key)?.isInvalidated).toBe(true);
    expect(client.getQueryState(other)?.isInvalidated).toBe(false);
  });

  it("refreshes one session's reads, not another's", async () => {
    const client = new QueryClient();
    const own = pullRequestRecordKey("pr-page-github-info", PR, sessionRecordOwner("conv_a"));
    const other = pullRequestRecordKey("pr-page-github-info", PR, sessionRecordOwner("conv_b"));
    for (const key of [own, other]) client.setQueryData(key, {});
    await invalidateSessionPullRequestReads(client, "conv_a", ["pr-page-github-info"]);
    expect(client.getQueryState(own)?.isInvalidated).toBe(true);
    expect(client.getQueryState(other)?.isInvalidated).toBe(false);
  });
});
