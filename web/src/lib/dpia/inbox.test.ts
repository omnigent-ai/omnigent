import { describe, expect, it } from "vitest";
import { buildManualCorrectionProposal } from "./correctionProposal";
import { recordDpiaLiveRun, stageCorrectionProposal } from "./dpiaApi";
import { countDpiaAttentionItems, deriveDpiaAttentionItems } from "./inbox";
import { createStudentSuccessAlertSeed } from "./seed";

describe("DPIA attention items", () => {
  it("derives proposals, stale findings, failed live runs, and an awaited decision", () => {
    const seed = createStudentSuccessAlertSeed();
    const proposal = buildManualCorrectionProposal(seed, {
      factId: "hosting",
      proposedValue: "The model and database are hosted in London.",
      evidenceId: "ev-response-security",
      instruction: "Correct the hosting fact using the reviewed security response.",
      rationale: "Hosting changes the vendor and international-transfer assessment basis.",
    });
    const staged = stageCorrectionProposal(seed, proposal, "manual", "2026-08-21T12:00:00Z");
    const failed = recordDpiaLiveRun(
      {
        ...staged,
        updatedAt: "2026-08-21T12:05:00Z",
        determinations: staged.determinations.map((finding) =>
          finding.id === "det-vendor"
            ? { ...finding, status: "stale-after-change", staleReason: "Hosting changed." }
            : finding,
        ),
      },
      {
        status: "failed",
        message: "The labelled root session stopped.",
        updatedAt: "2026-08-21T12:10:00Z",
      },
    );

    const items = deriveDpiaAttentionItems(failed);
    expect(items.map(({ kind }) => kind).sort()).toEqual([
      "awaited-decision",
      "failed-live-run",
      "pending-proposal",
      "stale-finding",
    ]);
    expect(items.find(({ kind }) => kind === "pending-proposal")?.href).toContain(
      "/dpia/cases/student-success-alert",
    );
    expect(countDpiaAttentionItems([failed])).toBe(4);
  });
});
