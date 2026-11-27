// The host's merge queue answer, read defensively: anything that isn't one for this PR is null.

import { describe, expect, it } from "vitest";

import { parseMergeQueueState } from "./hostMergeQueue";

const PR = "https://github.com/acme/app/pull/7";
const answer = (over: Record<string, unknown> = {}) => ({
  object: "session.github.merge_queue",
  pr_url: PR,
  enabled: true,
  queued: true,
  position: 2,
  state: "QUEUED",
  head_sha: "abc",
  ...over,
});

describe("parseMergeQueueState", () => {
  it("reads a queued PR's place and state", () => {
    expect(parseMergeQueueState(answer(), PR)).toEqual({
      prUrl: PR,
      enabled: true,
      queued: true,
      position: 2,
      state: "QUEUED",
      headSha: "abc",
    });
  });

  it("keeps GitHub's unknowns, and drops the place of a PR that isn't queued", () => {
    expect(parseMergeQueueState(answer({ enabled: null, queued: false }), PR)).toMatchObject({
      enabled: null,
      queued: false,
      position: null,
      state: null,
    });
  });

  it.each([
    ["another object", answer({ object: "session.github.pr_merge" })],
    ["another PR", answer({ pr_url: "https://github.com/acme/app/pull/8" })],
    ["a non-boolean enabled", answer({ enabled: "yes" })],
    ["a non-boolean queued", answer({ queued: 1 })],
    ["no object", null],
    ["a list", []],
  ])("is null for %s", (_name, value) => {
    expect(parseMergeQueueState(value, PR)).toBeNull();
  });

  it("ignores a place that isn't a positive whole number", () => {
    expect(parseMergeQueueState(answer({ position: 0 }), PR)?.position).toBeNull();
    expect(parseMergeQueueState(answer({ position: "2" }), PR)?.position).toBeNull();
  });
});
