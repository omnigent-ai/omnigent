import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ClaudeQuestion } from "@/lib/askUserQuestion";
import { AskUserQuestionForm } from "./AskUserQuestionForm";

afterEach(() => {
  cleanup();
});

const SINGLE_SELECT_QUESTION: ClaudeQuestion = {
  question: "Which framework?",
  header: "Framework",
  multiSelect: false,
  options: [
    { label: "React", description: "Component-based UI library." },
    { label: "Vue", description: "Progressive framework.", recommended: true },
  ],
};

const MULTI_SELECT_QUESTION: ClaudeQuestion = {
  question: "Which features?",
  header: "Features",
  multiSelect: true,
  options: [
    { label: "Auth", description: "User accounts." },
    { label: "Billing", description: "Payments.", recommended: true },
    { label: "Search", description: "Full-text search." },
  ],
};

const NO_RECOMMENDATION_QUESTION: ClaudeQuestion = {
  question: "Pick one",
  header: "Header",
  multiSelect: false,
  options: [
    { label: "A", description: "a" },
    { label: "B", description: "b" },
  ],
};

describe("AskUserQuestionForm — recommended option", () => {
  it("shows a Recommended badge next to the marked option", () => {
    render(
      <AskUserQuestionForm
        questions={[SINGLE_SELECT_QUESTION]}
        onSubmit={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    const badges = screen.getAllByTestId("ask-user-question-recommended-badge");
    expect(badges).toHaveLength(1);
  });

  it("does not show a badge when no option is recommended", () => {
    render(
      <AskUserQuestionForm
        questions={[NO_RECOMMENDATION_QUESTION]}
        onSubmit={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("ask-user-question-recommended-badge")).toBeNull();
  });

  it("pre-selects the recommended option for single-select", () => {
    render(
      <AskUserQuestionForm
        questions={[SINGLE_SELECT_QUESTION]}
        onSubmit={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    const vueRadio = screen.getByRole("radio", { name: /Vue/ });
    const reactRadio = screen.getByRole("radio", { name: /React/ });
    expect(vueRadio).toBeChecked();
    expect(reactRadio).not.toBeChecked();
    // Submit is enabled immediately — the pre-selection counts as an answer.
    expect(screen.getByTestId("ask-user-question-submit")).not.toBeDisabled();
  });

  it("pre-selects the recommended option for multi-select", () => {
    render(
      <AskUserQuestionForm
        questions={[MULTI_SELECT_QUESTION]}
        onSubmit={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    const billingCheckbox = screen.getByRole("checkbox", { name: /Billing/ });
    const authCheckbox = screen.getByRole("checkbox", { name: /Auth/ });
    expect(billingCheckbox).toBeChecked();
    expect(authCheckbox).not.toBeChecked();
  });

  it("submits the pre-selected recommended answer when accepted as-is", () => {
    const onSubmit = vi.fn();
    render(
      <AskUserQuestionForm
        questions={[SINGLE_SELECT_QUESTION]}
        onSubmit={onSubmit}
        onReject={vi.fn()}
      />,
    );
    screen.getByTestId("ask-user-question-submit").click();
    expect(onSubmit).toHaveBeenCalledWith({ "Which framework?": "Vue" });
  });

  it("leaves no pre-selection when the question has no recommended option", () => {
    render(
      <AskUserQuestionForm
        questions={[NO_RECOMMENDATION_QUESTION]}
        onSubmit={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(screen.getByTestId("ask-user-question-submit")).toBeDisabled();
  });
});
