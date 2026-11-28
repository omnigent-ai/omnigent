import { describe, expect, it } from "vitest";

import {
  compactCount,
  pullRequestListStats,
  pullRequestListStatsBatches,
  movedHeads,
  pullRequestListStatsCell,
  pullRequestListStatsKey,
  snapshotFingerprint,
  type PullRequestListStats,
  type PullRequestListStatsGap,
  type PullRequestListStatsTarget,
} from "./pullRequestListStats";
import { EXAMPLE_DEVICE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// Example Host stand-in hosts are recognized by name, as a build that registers Example Host does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_DEVICE_HOST] });

function target(number: number, login = "ada", host = "github.com"): PullRequestListStatsTarget {
  return {
    url: `https://${host}/acme/app/pull/${number}`,
    source: { kind: "github-fast-track", account: { host, login } },
    repository: "acme/app",
    number,
    headSha: "head",
    open: true,
  };
}

describe("pullRequestListStatsBatches", () => {
  it("sends one batch per account, in first-seen order", () => {
    const batches = pullRequestListStatsBatches([
      target(1),
      target(2, "bob"),
      target(3, "ADA"),
      target(4, "ada", "github.example.com"),
    ]);
    expect(batches.map((batch) => [batch.source, batch.targets.map((t) => t.number)])).toEqual([
      [{ kind: "github-fast-track", account: { host: "github.com", login: "ada" } }, [1, 3]],
      [{ kind: "github-fast-track", account: { host: "github.com", login: "bob" } }, [2]],
      [{ kind: "github-fast-track", account: { host: "github.example.com", login: "ada" } }, [4]],
    ]);
  });

  it("batches the session's host reads per session and host, apart from fast-track accounts", () => {
    const onHost = (number: number, sessionId: string): PullRequestListStatsTarget => ({
      ...target(number),
      source: { kind: "host", sessionId, host: "github.com" },
    });
    const batches = pullRequestListStatsBatches([
      onHost(1, "s1"),
      target(2),
      onHost(3, "s2"),
      onHost(4, "s1"),
    ]);
    expect(batches.map((batch) => batch.targets.map((t) => t.number))).toEqual([[1, 4], [2], [3]]);
  });

  it("splits a long list into batches no larger than the limit", () => {
    const targets = Array.from({ length: 7 }, (_, index) => target(index + 1));
    expect(
      pullRequestListStatsBatches(targets, 3).map((batch) => batch.targets.map((t) => t.number)),
    ).toEqual([[1, 2, 3], [4, 5, 6], [7]]);
    expect(pullRequestListStatsBatches([])).toEqual([]);
  });
});

describe("pullRequestListStats", () => {
  it("reads counts and GitHub's update time", () => {
    expect(
      pullRequestListStats(
        {
          repository: "acme/app",
          number: 1,
          additions: 12,
          deletions: 0,
          changed_files: 2,
          updated_at: "2026-11-03T10:00:00Z",
          head_sha: "head",
        },
        5_000,
      ),
    ).toEqual({
      additions: 12,
      deletions: 0,
      changedFiles: 2,
      updatedAt: Date.parse("2026-11-03T10:00:00Z"),
      headSha: "head",
      dispatchedAt: 5_000,
      // An older shell's answer carries no state, draft or CI keys.
      extended: false,
    });
  });

  it("keeps the base commit where the shell reads it, and none from an empty one", () => {
    const entry = {
      repository: "acme/app",
      number: 1,
      additions: 1,
      deletions: 1,
      changed_files: 1,
      updated_at: null,
      head_sha: "head",
    };
    expect(pullRequestListStats({ ...entry, base_sha: "base" })).toMatchObject({
      headSha: "head",
      baseSha: "base",
    });
    expect(pullRequestListStats({ ...entry, base_sha: "" })).not.toHaveProperty("baseSha");
    expect(pullRequestListStats({ ...entry, base_sha: null })).not.toHaveProperty("baseSha");
  });

  it("reads state, draft and CI, keeping a null rollup apart from absent fields", () => {
    const entry = {
      repository: "acme/app",
      number: 1,
      additions: 1,
      deletions: 1,
      changed_files: 1,
      updated_at: null,
      head_sha: "head",
    };
    expect(
      pullRequestListStats({
        ...entry,
        state: "MERGED",
        is_draft: false,
        ci: "failing",
        ci_head_sha: "head",
      }),
    ).toMatchObject({
      extended: true,
      state: "MERGED",
      isDraft: false,
      ci: "failing",
      ciHeadSha: "head",
    });
    const noRollup = pullRequestListStats({
      ...entry,
      state: "OPEN",
      is_draft: true,
      ci: null,
      ci_head_sha: null,
    });
    expect(noRollup).toMatchObject({ extended: true, state: "OPEN", isDraft: true, ci: null });
    expect(noRollup).not.toHaveProperty("ciHeadSha");
    // Unknown values from a newer shell read as unknown, not as a state.
    const unknown = pullRequestListStats({
      ...entry,
      state: "QUEUED",
      is_draft: null,
      ci: "neutral",
    });
    expect(unknown).toMatchObject({ extended: true, ci: null });
    expect(unknown).not.toHaveProperty("state");
    expect(unknown).not.toHaveProperty("isDraft");
  });

  it("leaves out what GitHub didn't answer or answered malformed", () => {
    expect(pullRequestListStats(null)).toBeUndefined();
    expect(
      pullRequestListStats({
        repository: "acme/app",
        number: 1,
        additions: null,
        deletions: -1,
        changed_files: 1.5,
        updated_at: "not a date",
        head_sha: null,
      }),
    ).toEqual({
      additions: undefined,
      deletions: undefined,
      changedFiles: undefined,
      updatedAt: undefined,
      headSha: undefined,
      dispatchedAt: 0,
      extended: false,
    });
  });
});

describe("movedHeads", () => {
  it("names the PRs whose host head moved since the read began", () => {
    const batch = {
      source: target(1).source,
      targets: [target(1), { ...target(2), headSha: "pushed" }, target(3)],
    };
    const hostHeads = new Map<string, string | undefined>([
      [target(1).url, "head"],
      [target(2).url, "head"],
    ]);
    // PR 3 joined the batch after the read; the batch's key change reads it anyway.
    expect(movedHeads(batch, { hostHeads })).toEqual([target(2).url]);
    expect(movedHeads(batch, undefined)).toEqual([]);
  });

  it("keys a batch by its source and PRs, not their heads", () => {
    const batch = { source: target(1).source, targets: [target(1)] };
    const pushed = { ...batch, targets: [{ ...target(1), headSha: "pushed" }] };
    expect(pullRequestListStatsKey(pushed)).toEqual(pullRequestListStatsKey(batch));
  });

  it("keys a host batch by its session and host", () => {
    const key = pullRequestListStatsKey({
      source: { kind: "host", sessionId: "s1", host: "GitHub.com" },
      targets: [target(1)],
    });
    expect(key).toEqual([
      "pull-request-host",
      "s1",
      "pull_request_list_stats",
      "github.com",
      "acme/app#1",
    ]);
  });
});

describe("snapshotFingerprint", () => {
  const entry = (snapshot: Record<string, unknown> | undefined, fields = {}) =>
    ({
      repository: "acme/app",
      number: 1,
      head_sha: "head",
      updated_at: "2026-11-11T10:00:00Z",
      state: "OPEN",
      is_draft: false,
      ...(snapshot ? { snapshot } : {}),
      ...fields,
    }) as Parameters<typeof snapshotFingerprint>[0];
  const whole = {
    check_runs: { SUCCESS: 2, IN_PROGRESS: 1 },
    status_contexts: {},
    review_decision: null,
    review_requests: 1,
    latest_reviews: 0,
  };

  it("matches for the same snapshot, whatever order GitHub counted states in", () => {
    const reordered = { ...whole, check_runs: { IN_PROGRESS: 1, SUCCESS: 2 } };
    expect(snapshotFingerprint(entry(reordered))).toBe(snapshotFingerprint(entry(whole)));
  });

  it.each([
    ["a check finishing", { snapshot: { ...whole, check_runs: { SUCCESS: 3 } } }],
    ["a review", { snapshot: { ...whole, latest_reviews: 1 } }],
    ["a decision", { snapshot: { ...whole, review_decision: "APPROVED" } }],
    ["a push", { head_sha: "pushed" }],
    ["a comment moving the update time", { updated_at: "2026-11-11T10:05:00Z" }],
    ["a merge", { state: "MERGED" }],
    ["leaving draft", { is_draft: true }],
  ])("changes with %s", (_change, fields) => {
    expect(snapshotFingerprint(entry(whole, fields))).not.toBe(snapshotFingerprint(entry(whole)));
  });

  it("keeps an explicit null decision and a head with no checks", () => {
    const none = { ...whole, check_runs: {}, status_contexts: {}, review_decision: null };
    expect(snapshotFingerprint(entry(none))).toBeDefined();
    expect(snapshotFingerprint(entry(whole, { updated_at: null }))).toBeDefined();
  });

  const { review_decision: _decision, ...withoutDecision } = whole;
  it.each([
    ["no snapshot, as an older shell answers", undefined, {}],
    ["no review decision", withoutDecision, {}],
    ["a decision that isn't text", { ...whole, review_decision: 1 }, {}],
    ["no check counts", { ...whole, check_runs: undefined }, {}],
    ["check counts as a list", { ...whole, check_runs: [] }, {}],
    ["a count that isn't a whole number", { ...whole, check_runs: { SUCCESS: 1.5 } }, {}],
    ["a negative count", { ...whole, status_contexts: { PENDING: -1 } }, {}],
    ["no review totals", { ...whole, review_requests: null }, {}],
    ["no head", whole, { head_sha: null }],
    ["no draft flag", whole, { is_draft: null }],
    ["no state", whole, { state: undefined }],
    ["no update time", whole, { updated_at: undefined }],
  ])("is absent for an answer with %s", (_case, snapshot, fields) => {
    expect(snapshotFingerprint(entry(snapshot, fields))).toBeUndefined();
  });

  it("is absent from stats with no snapshot, and set from a whole one", () => {
    expect(pullRequestListStats(entry(undefined))?.fingerprint).toBeUndefined();
    expect(pullRequestListStats(entry(whole))?.fingerprint).toBe(snapshotFingerprint(entry(whole)));
  });
});

describe("compactCount", () => {
  it.each([
    [0, "0"],
    [999, "999"],
    [1_000, "1k"],
    [2_240, "2.2k"],
    [9_940, "9.9k"],
    [10_000, "10k"],
    [123_456, "123k"],
  ])("shows %i as %s", (value, expected) => {
    expect(compactCount(value)).toBe(expected);
  });
});

describe("pullRequestListStatsCell", () => {
  const stats = (over: Partial<PullRequestListStats> = {}): PullRequestListStats => ({
    additions: 4,
    deletions: 2,
    changedFiles: 1,
    updatedAt: 1_000,
    headSha: "head",
    dispatchedAt: 0,
    extended: true,
    ...over,
  });

  it("shows each field it has, zero included", () => {
    const zero = stats({ additions: 0, deletions: 0 });
    expect(pullRequestListStatsCell("diff", zero, undefined, "github")).toEqual({
      kind: "value",
      stats: zero,
    });
    expect(pullRequestListStatsCell("updated", zero, undefined, "github").kind).toBe("value");
  });

  it("names the service that answered without a field", () => {
    expect(
      pullRequestListStatsCell("updated", stats({ updatedAt: undefined }), undefined, "named-host"),
    ).toEqual({
      kind: "gap",
      label: "Update time unavailable",
      reason: "Example Host didn't report it.",
    });
    expect(
      pullRequestListStatsCell("diff", stats({ deletions: undefined }), undefined, "github"),
    ).toEqual({ kind: "gap", label: "Diff size unavailable", reason: "GitHub didn't report it." });
  });

  it("stays blank while a read may still answer", () => {
    expect(pullRequestListStatsCell("diff", undefined, undefined, "github")).toEqual({
      kind: "blank",
    });
  });

  it.each<[PullRequestListStatsGap, string]>([
    [{ kind: "outdated", service: "github" }, "Update the app to see it here."],
    [{ kind: "unavailable", service: "github" }, "This app can't read it on this Mac."],
    [{ kind: "no-account" }, "The session's host didn't report a GitHub account."],
    [{ kind: "unsupported-host" }, "This code host doesn't report it."],
    [{ kind: "not-reported", service: "named-host" }, "Example Host didn't report it."],
    [
      { kind: "read-failed", service: "github", code: "rate_limited", message: "Try later." },
      "GitHub is limiting requests from this Mac.",
    ],
    [
      {
        kind: "read-failed",
        service: "named-host",
        code: "certificate_rejected",
        message: "Renew it.",
      },
      "Renew it.",
    ],
    // Example Host's own rate limit keeps Example Host's words, never GitHub's.
    [
      {
        kind: "read-failed",
        service: "named-host",
        code: "rate_limited",
        message: "Example Host is busy.",
      },
      "Example Host is busy.",
    ],
    [
      { kind: "read-failed", service: "named-host", code: "brand_new", message: undefined },
      "Couldn't read it from Example Host.",
    ],
  ])("gives %o the reason %s", (gap, reason) => {
    expect(pullRequestListStatsCell("diff", undefined, gap, undefined)).toEqual({
      kind: "gap",
      label: "Diff size unavailable",
      reason,
    });
  });
});
