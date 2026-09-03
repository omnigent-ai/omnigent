import { describe, expect, it } from "vitest";
import { applyCorrectionProposal, recordOfficerDecision, stageCorrectionProposal } from "./dpiaApi";
import { parseCorrectionProposalText } from "./correctionProposal";
import { correctionProposalSchema } from "./schemas";
import { createStudentSuccessAlertSeed } from "./seed";

const validProposal = {
  artifact: "correction-proposal",
  case_id: "student-success-alert",
  processing_model_version: 3,
  policy_pack_version: "uk-dpia-2026.08-demo.1",
  instruction: "Correct the model and database hosting fact using the security response.",
  target_facts: [
    {
      fact_id: "hosting",
      current_value: "",
      proposed_value: "The model and primary database are hosted in London.",
    },
  ],
  new_evidence_refs: [
    {
      evidence_id: "ev-response-security",
      excerpt: "The model and primary database are hosted in London.",
    },
  ],
  affected_finding_ids: ["det-vendor"],
  expected_version_bump: { from: 3, to: 4 },
  stale_finding_ids: ["det-vendor"],
  role_to_reassess: "privacy_assessor",
  rationale: "Hosting changes the vendor and international-transfer assessment basis.",
} as const;

describe("correction proposal schema", () => {
  it("parses a strict current-to-proposed fact correction", () => {
    expect(correctionProposalSchema.parse(validProposal)).toEqual(validProposal);
    expect(parseCorrectionProposalText(JSON.stringify(validProposal))).toEqual(validProposal);
    expect(
      parseCorrectionProposalText(
        JSON.stringify({
          ...validProposal,
          new_evidence_refs: [{ evidence_id: "EV-12", excerpt: "Synthetic evidence" }],
        }),
      )?.new_evidence_refs,
    ).toEqual([{ evidence_id: "ev-response-security", excerpt: "Synthetic evidence" }]);
    expect(parseCorrectionProposalText(`Proposal: ${JSON.stringify(validProposal)}`)).toBeNull();
  });

  it("refuses schema-external fields and duplicate finding ids", () => {
    expect(() => correctionProposalSchema.parse({ ...validProposal, auto_apply: true })).toThrow();
    expect(() =>
      correctionProposalSchema.parse({
        ...validProposal,
        affected_finding_ids: ["det-vendor", "det-vendor"],
      }),
    ).toThrow(/unique/i);
  });

  it("refuses inconsistent version math and stale findings outside the affected set", () => {
    expect(() =>
      correctionProposalSchema.parse({
        ...validProposal,
        expected_version_bump: { from: 2, to: 8 },
        stale_finding_ids: ["det-security"],
      }),
    ).toThrow(/version|stale/i);
  });

  it("applies through the versioned intake mutation and records distinct audit events", () => {
    const seed = createStudentSuccessAlertSeed();
    const decided = recordOfficerDecision(seed, {
      action: "accepted",
      outcome: "full-dpia-likely",
      rationale: "The current evidence supports progressing while the known gaps are resolved.",
      officer: "Alex Morgan",
      decidedAt: "2026-08-21T12:00:00Z",
      processingModelVersion: 3,
      policyPackVersion: seed.policyPack.version,
    });
    const staged = stageCorrectionProposal(decided, validProposal, "agent", "2026-08-21T12:05:00Z");
    const updated = applyCorrectionProposal(
      staged,
      validProposal,
      "Alex Morgan",
      "2026-08-21T12:10:00Z",
    );

    expect(updated.processingModel.version).toBe(4);
    expect(updated.officerDecision).toBeUndefined();
    expect(updated.processingModel.facts.find(({ id }) => id === "hosting")).toMatchObject({
      value: "The model and primary database are hosted in London.",
      evidenceIds: ["ev-response-security"],
    });
    expect(updated.determinations.find(({ id }) => id === "det-vendor")).toMatchObject({
      status: "stale-after-change",
    });
    expect(
      updated.determinations
        .filter(({ status }) => status === "stale-after-change")
        .map(({ id }) => id),
    ).toEqual(["det-vendor"]);
    expect(
      updated.determinations
        .filter(({ status }) => status !== "stale-after-change")
        .every(({ processingModelVersion }) => processingModelVersion === 4),
    ).toBe(true);
    expect(updated.correctionProposals?.[0]).toMatchObject({ status: "applied" });
    expect(updated.audit).toHaveLength(staged.audit.length + 2);
    expect(updated.audit.at(-2)).toMatchObject({
      action: "Updated material intake facts",
      newValue: "Processing model v4",
    });
    expect(updated.audit.at(-1)).toMatchObject({
      action: "Applied officer-approved correction proposal",
      newValue: expect.stringContaining("Processing model v4"),
    });
    expect(updated.audit.at(-1)?.priorValue).toContain(validProposal.instruction);
    expect(updated.audit.at(-1)?.priorValue).toContain('"artifact":"correction-proposal"');
  });
});
