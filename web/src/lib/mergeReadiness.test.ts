import { describe, expect, it } from "vitest";
import {
  MERGE_READINESS_DETAILS,
  type MergeReadinessReasons,
  type MergeReadinessStatus,
  type MergeReadinessWording,
  presentMergeReadinessView,
} from "@/lib/mergeReadiness";

const failure = { label: "Unavailable", short: "Unavailable", tone: "gray" } as const;
const wording = (details: MergeReadinessWording["details"]): MergeReadinessWording => ({
  details,
  failures: { credential: failure, service: failure, github: failure },
});
const known = (
  status: MergeReadinessStatus,
  reasons: MergeReadinessReasons = { failingJobs: [], failingRequirements: [] },
) => ({ kind: "known", readiness: { status, reasons }, headSha: "abc123" }) as const;

describe("presentMergeReadinessView", () => {
  it.each(["checks_failing", "ci_failing", "policy_failing"] as const)(
    "gives %s the source's own reason when its reasons name nothing",
    (status) => {
      const own = wording({ [status]: "Something blocking failed." });
      expect(presentMergeReadinessView(known(status), own).detail).toBe(
        "Something blocking failed.",
      );
    },
  );

  it("keeps the shared reasons for a source that adds none of its own", () => {
    expect(presentMergeReadinessView(known("conflicts"), wording({})).detail).toBe(
      MERGE_READINESS_DETAILS.conflicts,
    );
  });

  it("uses a source's own reason in place of the shared one", () => {
    const own = wording({ conflicts: "Rebase onto the base branch." });
    expect(presentMergeReadinessView(known("conflicts"), own).detail).toBe(
      "Rebase onto the base branch.",
    );
  });

  it("names failing jobs and requirements, and gives no detail when none are named", () => {
    const reasons = { failingJobs: ["build"], failingRequirements: ["owners"] };
    expect(presentMergeReadinessView(known("checks_failing", reasons), wording({})).detail).toBe(
      "Blocking jobs failing: build. Requirements failing: owners.",
    );
    expect(presentMergeReadinessView(known("checks_failing"), wording({})).detail).toBeUndefined();
  });
});
