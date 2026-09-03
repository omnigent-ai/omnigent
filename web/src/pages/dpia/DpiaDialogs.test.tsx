import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { createStudentSuccessAlertSeed } from "@/lib/dpia/seed";
import { DpiaIntakeDialog, OfficerDecisionDialog, QuestionAnswerDialog } from "./DpiaDialogs";

function deferredRejection() {
  let reject: (reason: Error) => void = () => undefined;
  const promise = new Promise<never>((_resolve, rejectPromise) => {
    reject = rejectPromise;
  });
  return { promise, reject };
}

describe("DPIA dialog persistence", () => {
  it("keeps intake values available and locks controls until a rejected save settles", async () => {
    const pending = deferredRejection();
    render(
      <DpiaIntakeDialog
        open
        onOpenChange={vi.fn()}
        caseData={createStudentSuccessAlertSeed()}
        onSave={() => pending.promise}
      />,
    );
    const purpose = screen.getByRole("textbox", { name: "Purpose" });
    fireEvent.change(purpose, { target: { value: "A durable test purpose" } });
    fireEvent.click(screen.getByRole("button", { name: "Save new version" }));

    expect(screen.getByRole("button", { name: "Saving" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    pending.reject(new Error("DPIA persistence unavailable"));

    expect(await screen.findByRole("alert")).toHaveTextContent("DPIA persistence unavailable");
    expect(screen.getByRole("textbox", { name: "Purpose" })).toHaveValue("A durable test purpose");
    expect(screen.getByRole("button", { name: "Save new version" })).toBeEnabled();
  });

  it("keeps a stakeholder answer available after a rejected save", async () => {
    const pending = deferredRejection();
    const question = createStudentSuccessAlertSeed().questions[0];
    render(
      <QuestionAnswerDialog
        question={question}
        onOpenChange={vi.fn()}
        onSubmit={() => pending.promise}
      />,
    );
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Stakeholder answer" }), {
      target: { value: "The service retains each alert for one academic term." },
    });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Answered by" }), {
      target: { value: "Jamie Patel" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Record answer" }));

    expect(within(dialog).getByRole("button", { name: "Recording" })).toBeDisabled();
    pending.reject(new Error("Answer persistence unavailable"));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Answer persistence unavailable",
    );
    expect(within(dialog).getByRole("textbox", { name: "Stakeholder answer" })).toHaveValue(
      "The service retains each alert for one academic term.",
    );
  });

  it("keeps an officer decision available after a rejected save", async () => {
    const pending = deferredRejection();
    render(
      <OfficerDecisionDialog
        action="accepted"
        caseData={createStudentSuccessAlertSeed()}
        onOpenChange={vi.fn()}
        onSubmit={() => pending.promise}
      />,
    );
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Record decision" }));

    expect(within(dialog).getByRole("button", { name: "Recording" })).toBeDisabled();
    pending.reject(new Error("Decision persistence unavailable"));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Decision persistence unavailable",
    );
    await waitFor(() =>
      expect(within(dialog).getByRole("button", { name: "Record decision" })).toBeEnabled(),
    );
  });
});
