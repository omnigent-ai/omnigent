// The page's rule for retrying a bulk run's change, against the same cases main's grant follows,
// and the loop that sends it again: how long it waits, how often, and when it stops.

import { describe, expect, it, vi } from "vitest";

import settlement from "../../electron/test/fixtures/runGrantSettlementCases.json";

import {
  BULK_RETRY_LIMIT,
  bulkRetryAllowed,
  bulkRetryDelayMs,
  landedEarlier,
  withBulkRetries,
} from "./macWriteRetries";

describe("bulkRetryAllowed", () => {
  // Only the answers a page can see: main's own steps record decides the rest the same way.
  for (const testCase of settlement.cases.filter((each) => each.pageSees)) {
    const retries = testCase.decision === "rearm" && testCase.code !== "done";
    it(`${testCase.name}: ${retries ? "retries" : "stops"}`, () => {
      expect(
        bulkRetryAllowed(
          { code: testCase.code, cause: testCase.cause },
          { rechecksBeforeWrite: testCase.rechecksBeforeWrite, batch: testCase.batch },
        ),
      ).toBe(retries);
    });
  }
});

describe("landedEarlier", () => {
  it("reads only a state change already made as the earlier send's", () => {
    for (const code of ["already_closed", "already_open", "already_ready", "already_draft"]) {
      expect(landedEarlier(code)).toBe(true);
    }
    for (const code of ["already_on", "rejected", "not_open", "merged"]) {
      expect(landedEarlier(code)).toBe(false);
    }
  });
});

describe("bulkRetryDelayMs", () => {
  it("waits 1, 3 and 9 seconds, and a rate limit 10 and 30, each spread by half", () => {
    const at = (random: number) =>
      [1, 2, 3].map((retry) => bulkRetryDelayMs("busy", retry, () => random));
    expect(at(0.5)).toEqual([1_000, 3_000, 9_000]);
    expect(at(0)).toEqual([500, 1_500, 4_500]);
    expect(at(1)).toEqual([1_500, 4_500, 13_500]);
    expect([1, 2, 3].map((retry) => bulkRetryDelayMs("rate_limited", retry, () => 0.5))).toEqual([
      10_000, 30_000, 30_000,
    ]);
  });
});

describe("withBulkRetries", () => {
  type Answer = { ok: true } | { ok: false; code: string; cause?: string };
  const failureOf = (answer: Answer) =>
    answer.ok ? null : { code: answer.code, cause: answer.cause ?? null };
  const answers = (...sequence: Answer[]) => {
    const send = vi.fn(async () => sequence.shift() ?? { ok: true as const });
    return send;
  };
  const noWait = { sleep: vi.fn(async () => {}), random: () => 0.5 };

  it("sends again after a passing failure, and stops at the first success", async () => {
    const send = answers({ ok: false, code: "rate_limited" }, { ok: true });
    const result = await withBulkRetries(send, failureOf, {
      rechecksBeforeWrite: false,
      mayRetry: () => true,
      ...noWait,
    });
    expect(result).toEqual({ outcome: { ok: true }, afterUnknown: false });
    expect(send).toHaveBeenCalledTimes(2);
    expect(noWait.sleep).toHaveBeenLastCalledWith(10_000);
  });

  it(`sends at most ${BULK_RETRY_LIMIT} more times`, async () => {
    const send = vi.fn(async (): Promise<Answer> => ({ ok: false, code: "busy" }));
    const result = await withBulkRetries(send, failureOf, {
      rechecksBeforeWrite: false,
      mayRetry: () => true,
      ...noWait,
    });
    expect(result.outcome).toEqual({ ok: false, code: "busy" });
    expect(send).toHaveBeenCalledTimes(1 + BULK_RETRY_LIMIT);
  });

  it("never sends again what would only be refused again, or an unknown change that isn't re-read", async () => {
    for (const answer of [
      { ok: false as const, code: "forbidden" },
      { ok: false as const, code: "query_failed", cause: "sso_required" },
      { ok: false as const, code: "outcome_unknown" },
    ]) {
      const send = answers(answer);
      // oxlint-disable-next-line no-await-in-loop -- names the failing case
      await withBulkRetries(send, failureOf, {
        rechecksBeforeWrite: false,
        mayRetry: () => true,
        ...noWait,
      });
      expect(send).toHaveBeenCalledTimes(1);
    }
  });

  it("says when a send came after an unknown outcome", async () => {
    const send = answers(
      { ok: false, code: "outcome_unknown" },
      { ok: false, code: "already_ready" },
    );
    const result = await withBulkRetries(send, failureOf, {
      rechecksBeforeWrite: true,
      mayRetry: () => true,
      ...noWait,
    });
    expect(result).toEqual({ outcome: { ok: false, code: "already_ready" }, afterUnknown: true });
  });

  it("stops before or after its wait once a retry may no longer go", async () => {
    const before = answers({ ok: false, code: "busy" });
    await withBulkRetries(before, failureOf, {
      rechecksBeforeWrite: false,
      mayRetry: () => false,
      ...noWait,
    });
    expect(before).toHaveBeenCalledTimes(1);
    let allowed = true;
    const during = answers({ ok: false, code: "busy" });
    await withBulkRetries(during, failureOf, {
      rechecksBeforeWrite: false,
      mayRetry: () => allowed,
      sleep: async () => {
        allowed = false;
      },
    });
    expect(during).toHaveBeenCalledTimes(1);
  });
});
