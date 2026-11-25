// What the Merge section does for a PR's merge queue, from this Mac's merge options and
// what the shell offers: only an explicit membership and a known method enable anything.

import { describe, expect, it, vi } from "vitest";

import type { MacWriteRoute } from "@/lib/desktopPullRequestWrites";
import { macMergeQueueState, type PullRequestMergeOptions } from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const ROUTE = {
  target: { provider: "github", host: "github.com", repository: "acme/app", number: 42 },
  actor: { login: "mac-octo", generation: 1 },
} as unknown as MacWriteRoute;
const BOTH = { add: true, remove: true };
const options = (fields: Partial<PullRequestMergeOptions>): PullRequestMergeOptions => ({
  object: "session.github.merge_options",
  methods: ["squash"],
  default: "squash",
  merge_queue: true,
  merge_queue_membership: "not_queued",
  merge_queue_method: "squash",
  ...fields,
});
const queued = {
  merge_queue_membership: "queued",
  merge_queue_entry: { position: 1, state: "awaiting_checks" },
} as const;

describe("macMergeQueueState", () => {
  it("leaves the host path and a base without a queue alone", () => {
    expect(macMergeQueueState({ route: null, options: options({}), offers: BOTH })).toEqual({
      kind: "host",
    });
    expect(
      macMergeQueueState({ route: ROUTE, options: options({ merge_queue: false }), offers: BOTH }),
    ).toEqual({ kind: "none" });
  });

  it("offers Add only for an explicit not-queued answer with a known method", () => {
    expect(macMergeQueueState({ route: ROUTE, options: options({}), offers: BOTH })).toEqual({
      kind: "add",
      method: "squash",
    });
  });

  it.each([
    ["the membership is absent, from an older shell", { merge_queue_membership: undefined }],
    ["the membership is unknown", { merge_queue_membership: "unknown" as const }],
    ["the method is unknown", { merge_queue_method: "unknown" as const }],
    ["the method is absent", { merge_queue_method: undefined }],
    ["queued without an entry", { merge_queue_membership: "queued" as const }],
  ])("sends the user to GitHub when %s", (_label, fields) => {
    expect(macMergeQueueState({ route: ROUTE, options: options(fields), offers: BOTH })).toEqual({
      kind: "legacy",
      known: true,
    });
  });

  it.each([
    ["not queued, yet with an entry", { merge_queue_entry: { position: 0, state: "queued" } }],
    [
      "queued with a string position",
      { ...queued, merge_queue_entry: { position: "1", state: "queued" } },
    ],
    [
      "queued with a negative position",
      { ...queued, merge_queue_entry: { position: -1, state: "queued" } },
    ],
    [
      "queued with a state it doesn't know",
      { ...queued, merge_queue_entry: { position: 1, state: "paused" } },
    ],
    ["queued with an array entry", { ...queued, merge_queue_entry: [] }],
    ["a membership it doesn't know", { merge_queue_membership: "pending" }],
  ])("acts on nothing when the bridge sends %s", (_label, fields) => {
    expect(
      macMergeQueueState({
        route: ROUTE,
        options: options(fields as unknown as Partial<PullRequestMergeOptions>),
        offers: BOTH,
      }),
    ).toEqual({ kind: "legacy", known: true });
  });

  it("says it couldn't tell when GitHub can't say whether there's a queue, or it isn't read yet", () => {
    for (const read of [
      options({ merge_queue: null }),
      options({ merge_queue: undefined }),
      undefined,
    ]) {
      expect(macMergeQueueState({ route: ROUTE, options: read, offers: BOTH })).toEqual({
        kind: "legacy",
        known: false,
      });
    }
  });

  it("sends the user to GitHub when the shell doesn't offer Add, even for a known not-queued PR", () => {
    expect(
      macMergeQueueState({
        route: ROUTE,
        options: options({}),
        offers: { add: false, remove: true },
      }),
    ).toEqual({ kind: "legacy", known: true });
  });

  it("shows a queued PR whatever is offered, removable only where Remove is", () => {
    expect(
      macMergeQueueState({
        route: ROUTE,
        options: options(queued),
        offers: { add: true, remove: false },
      }),
    ).toEqual({ kind: "queued", position: 1, state: "awaiting_checks", removable: false });
    expect(
      macMergeQueueState({
        route: ROUTE,
        options: options(queued),
        offers: { add: false, remove: true },
      }),
    ).toEqual({ kind: "queued", position: 1, state: "awaiting_checks", removable: true });
  });
});
