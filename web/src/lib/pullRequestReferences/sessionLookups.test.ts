import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import {
  createSessionReferenceLookups,
  FAILED_RETRY_MS,
  followSharedReferenceAnswers,
  MAX_ATTEMPTS_PER_REFERENCE,
  type SessionReferenceLookups,
} from "./sessionLookups";
import type { ReferenceLookup } from "./useReferencedPullRequest";

const FOUND: ReferenceLookup = {
  kind: "found",
  pr: {
    url: "u",
    number: 1,
    title: "t",
    state: "OPEN",
    is_draft: false,
    repository: "acme/app",
    updated_at: null,
  },
  readAt: 1_000,
};

/** Sends `key`'s lookup the way the view does: only where the record doesn't answer it. */
function lookUp(
  { budget, record }: SessionReferenceLookups,
  key: string,
  url: string,
  now: number,
  answer: ReferenceLookup,
): "sent" | "answered by the record" | "over the cap" {
  if (!budget.admit(key, url)) return "over the cap";
  if (record.insteadOfLookup(key, now)) return "answered by the record";
  record.sent(key);
  record.answered(key, answer);
  return "sent";
}

describe("createSessionReferenceLookups", () => {
  it("sends each reference once, and answers every later read from what came back", () => {
    const lookups = createSessionReferenceLookups();
    expect(lookUp(lookups, "a", "https://x/1", 0, FOUND)).toBe("sent");
    // However stale it's become, or wherever the cache let it go.
    expect(lookUp(lookups, "a", "https://x/1", 60 * 60_000, FOUND)).toBe("answered by the record");
    expect(lookups.record.remembered("a")).toEqual(FOUND);
    const missing: ReferenceLookup = { kind: "missing", readAt: 0 };
    expect(lookUp(lookups, "b", "https://x/2", 0, missing)).toBe("sent");
    expect(lookups.record.insteadOfLookup("b", 60 * 60_000)).toEqual(missing);
  });

  it("retries a failure once, and only once it has waited", () => {
    const lookups = createSessionReferenceLookups();
    const failed: ReferenceLookup = { kind: "failed", readAt: 1_000 };
    expect(lookUp(lookups, "a", "https://x/1", 1_000, failed)).toBe("sent");
    expect(lookUp(lookups, "a", "https://x/1", 1_000 + FAILED_RETRY_MS - 1, failed)).toBe(
      "answered by the record",
    );
    const again: ReferenceLookup = { kind: "failed", readAt: 1_000 + FAILED_RETRY_MS };
    expect(lookUp(lookups, "a", "https://x/1", again.readAt, again)).toBe("sent");
    expect(lookUp(lookups, "a", "https://x/1", again.readAt + 10 * FAILED_RETRY_MS, again)).toBe(
      "answered by the record",
    );
  });

  it("keeps the newest answer, whoever read it, and a failure never replaces one", () => {
    const { record } = createSessionReferenceLookups();
    const newer: ReferenceLookup = { ...FOUND, readAt: FOUND.readAt + 1 };
    record.answered("a", newer);
    record.answered("a", FOUND);
    record.answered("a", { kind: "failed", readAt: FOUND.readAt + 2 });
    expect(record.remembered("a")).toEqual(newer);
    // What another read said stands in for this view's own, so it never sends one.
    expect(record.insteadOfLookup("a", 0)).toEqual(newer);
  });

  it("follows a PR it only watches, and still sends its own lookup while it has no answer", () => {
    const { record } = createSessionReferenceLookups();
    expect(record.tracks("a")).toBe(false);
    record.watch("a");
    expect(record.tracks("a")).toBe(true);
    expect(record.insteadOfLookup("a", 0)).toBeUndefined();
    record.answered("a", FOUND);
    record.watch("a");
    expect(record.insteadOfLookup("a", 0)).toEqual(FOUND);
  });

  it("counts nothing for a lookup held before it went out", () => {
    const { budget, record } = createSessionReferenceLookups();
    expect(budget.admit("a", "https://x/1")).toBe(true);
    // Held in the queue: never sent, so it may still go out later.
    expect(record.insteadOfLookup("a", 0)).toBeUndefined();
    expect(record.remembered("a")).toBeUndefined();
  });

  it("caps a view at 60 PRs, reading one again as another account without more of the cap", () => {
    const lookups = createSessionReferenceLookups();
    for (let number = 1; number <= 60; number += 1) {
      expect(lookUp(lookups, `old ${number}`, `https://x/${number}`, 0, FOUND)).toBe("sent");
    }
    expect(lookUp(lookups, "old 61", "https://x/61", 0, FOUND)).toBe("over the cap");
    // Another account's key for the same PR is a new read, and the old answer isn't its own.
    expect(lookups.record.remembered("new 1")).toBeUndefined();
    expect(lookUp(lookups, "new 1", "https://x/1", 0, FOUND)).toBe("sent");
    expect(lookUp(lookups, "new 61", "https://x/61", 0, FOUND)).toBe("over the cap");
  });

  it("sends at most 120 lookups as one account: 60 PRs, and one retry of each failure", () => {
    const lookups = createSessionReferenceLookups();
    let sent = 0;
    // Every lookup fails, and the view keeps asking, long past every retry's wait.
    for (let now = 0; now <= 10 * FAILED_RETRY_MS; now += FAILED_RETRY_MS) {
      for (let number = 1; number <= 100; number += 1) {
        const failed: ReferenceLookup = { kind: "failed", readAt: now };
        if (lookUp(lookups, `key ${number}`, `https://x/${number}`, now, failed) === "sent") {
          sent += 1;
        }
      }
    }
    expect(MAX_ATTEMPTS_PER_REFERENCE).toBe(2);
    expect(sent).toBe(120);
  });
});

describe("followSharedReferenceAnswers", () => {
  it("tells the record of newer answers for the PRs it read, and of nothing else", () => {
    const queryClient = new QueryClient();
    const { record } = createSessionReferenceLookups();
    const read = ["pull-request-reference", "https://x/1", "github.com", "mac-octo"];
    const other = ["pull-request-reference", "https://x/2", "github.com", "mac-octo"];
    record.sent(JSON.stringify(read));
    record.answered(JSON.stringify(read), FOUND);
    const stop = followSharedReferenceAnswers(queryClient, record);
    const newer: ReferenceLookup = { ...FOUND, readAt: FOUND.readAt + 1 };
    queryClient.setQueryData(read, newer);
    queryClient.setQueryData(other, newer);
    // An older answer, or a failure, written later still loses to what the record has.
    queryClient.setQueryData(read, FOUND);
    queryClient.setQueryData(read, { kind: "failed", readAt: FOUND.readAt + 5 });
    expect(record.remembered(JSON.stringify(read))).toEqual(newer);
    expect(record.tracks(JSON.stringify(other))).toBe(false);
    stop();
    const newest: ReferenceLookup = { ...FOUND, readAt: FOUND.readAt + 9 };
    queryClient.setQueryData(read, newest);
    expect(record.remembered(JSON.stringify(read))).toEqual(newer);
  });
});
