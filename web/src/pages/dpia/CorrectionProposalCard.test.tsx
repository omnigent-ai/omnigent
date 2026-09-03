import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { buildManualCorrectionProposal } from "@/lib/dpia/correctionProposal";
import { createStudentSuccessAlertSeed } from "@/lib/dpia/seed";
import type { CorrectionProposalRecord } from "@/lib/dpia/types";
import { CorrectionProposalCard, ManualCorrectionDialog } from "./CorrectionProposalCard";

afterEach(cleanup);

function proposalRecord(): {
  caseData: ReturnType<typeof createStudentSuccessAlertSeed>;
  record: CorrectionProposalRecord;
} {
  const caseData = createStudentSuccessAlertSeed();
  const proposal = buildManualCorrectionProposal(caseData, {
    factId: "hosting",
    proposedValue: "The model and primary database are hosted in London.",
    evidenceId: "ev-response-security",
    instruction: "Correct the hosting fact using the reviewed security response.",
    rationale: "Hosting changes the vendor and international-transfer assessment basis.",
  });
  return {
    caseData,
    record: {
      id: "correction-1",
      proposal,
      source: "agent",
      status: "pending",
      createdAt: "2026-08-21T12:00:00Z",
    },
  };
}

describe("CorrectionProposalCard", () => {
  it("shows the fact diff and exposes all officer decisions", () => {
    const { caseData, record } = proposalRecord();
    const onApply = vi.fn();
    const onEdit = vi.fn();
    const onReject = vi.fn();
    const onFollowUp = vi.fn();
    render(
      <CorrectionProposalCard
        caseData={caseData}
        record={record}
        onApply={onApply}
        onEdit={onEdit}
        onReject={onReject}
        onFollowUp={onFollowUp}
      />,
    );

    expect(screen.getByText("Not recorded")).toBeInTheDocument();
    expect(
      screen.getByText("The model and primary database are hosted in London."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    fireEvent.click(screen.getByRole("button", { name: "Follow-up" }));
    expect(onApply).toHaveBeenCalledOnce();
    expect(onEdit).toHaveBeenCalledOnce();
    expect(onReject).toHaveBeenCalledOnce();
    expect(onFollowUp).toHaveBeenCalledOnce();
  });

  it("builds the same strict proposal shape through the offline manual path", () => {
    const caseData = createStudentSuccessAlertSeed();
    const onSubmit = vi.fn();
    render(
      <ManualCorrectionDialog
        open
        onOpenChange={vi.fn()}
        caseData={caseData}
        onSubmit={onSubmit}
      />,
    );

    fireEvent.change(
      screen.getByRole("textbox", { name: "Proposed value for Model and database hosting" }),
      {
        target: { value: "The service and backups are hosted in the United Kingdom." },
      },
    );
    fireEvent.change(screen.getByRole("textbox", { name: "Correction rationale" }), {
      target: { value: "Confirmed UK hosting changes the transfer assessment basis." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create proposal" }));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        artifact: "correction-proposal",
        processing_model_version: 3,
        target_facts: [
          expect.objectContaining({
            fact_id: "hosting",
            proposed_value: "The service and backups are hosted in the United Kingdom.",
          }),
        ],
        expected_version_bump: { from: 3, to: 4 },
        stale_finding_ids: ["det-vendor"],
      }),
    );
  });
});
