import { describe, expect, it } from "vitest";

import {
  REVIEW_PLAN_FIRST,
  createWorkflowPrompt,
  runSavedWorkflowPrompt,
  offersSaveWorkflow,
  offersWorkflow,
  parseSavedWorkflowMessage,
  savedWorkflowMessage,
  saveWorkflowMessage,
  workflowMessage,
} from "./workflow";

describe("workflowMessage", () => {
  it("asks for the plan review in its own paragraph", () => {
    expect(workflowMessage("/", "  Add retries\n", true)).toBe(
      `/workflow Add retries\n\n${REVIEW_PLAN_FIRST}`,
    );
  });

  it("is the bare command without the review, in the harness's prefix", () => {
    expect(workflowMessage("$", "Add retries", false)).toBe("$workflow Add retries");
  });
});

describe("offersWorkflow", () => {
  it("needs the workflow skill", () => {
    expect(offersWorkflow([{ name: "deslop" }, { name: "workflow" }])).toBe(true);
    expect(offersWorkflow([{ name: "deslop" }])).toBe(false);
  });
});

describe("savedWorkflowMessage", () => {
  const workflow = { id: "0123456789abcdef0123456789abcdef", name: "Triage flaky tests" };

  it("names the saved workflow by ID, then the details and the review", () => {
    expect(savedWorkflowMessage("/", workflow, "  The upload tests ", true)).toBe(
      `/workflow Run the saved workflow "Triage flaky tests" (ID ${workflow.id}).\n\n` +
        `The upload tests\n\n${REVIEW_PLAN_FIRST}`,
    );
  });

  it("leaves out empty details", () => {
    expect(savedWorkflowMessage("$", workflow, " ", false)).toBe(
      `$workflow Run the saved workflow "Triage flaky tests" (ID ${workflow.id}).`,
    );
  });
});

describe("saveWorkflowMessage", () => {
  it("runs the save-workflow skill with the picked name", () => {
    expect(saveWorkflowMessage("/", " Triage flaky tests ")).toBe(
      '/save-workflow Save this session\'s plan as a workflow named "Triage flaky tests".',
    );
    expect(offersSaveWorkflow([{ name: "save-workflow" }])).toBe(true);
    expect(offersSaveWorkflow([{ name: "workflow" }])).toBe(false);
  });
});

describe("createWorkflowPrompt", () => {
  it("asks the save-workflow skill to draft the named workflow without doing the work", () => {
    expect(createWorkflowPrompt("  Triage flaky tests ", " Rerun the suite ")).toBe(
      'Use the save-workflow skill to create a workflow named "Triage flaky tests" from this description, without doing the work:\n\nRerun the suite',
    );
  });
});

describe("parseSavedWorkflowMessage", () => {
  const workflow = { id: "0123456789abcdef0123456789abcdef", name: 'Triage "flaky" tests' };

  it("reads back a saved workflow's message, with or without details", () => {
    expect(
      parseSavedWorkflowMessage(savedWorkflowMessage("$", workflow, " The upload tests ", false)),
    ).toEqual({ ...workflow, details: "The upload tests" });
    expect(parseSavedWorkflowMessage(savedWorkflowMessage("/", workflow, "", false))).toEqual({
      ...workflow,
      details: "",
    });
  });

  it("ignores other prompts", () => {
    expect(parseSavedWorkflowMessage("Summarize overnight activity")).toBeNull();
    expect(parseSavedWorkflowMessage("/workflow Add retries")).toBeNull();
  });
});

describe("runSavedWorkflowPrompt", () => {
  it("asks the workflow skill to run the saved workflow by ID, with the plan review", () => {
    expect(
      runSavedWorkflowPrompt({
        id: "0123456789abcdef0123456789abcdef",
        name: "Triage flaky tests",
      }),
    ).toBe(
      `Use the workflow skill to run the saved workflow "Triage flaky tests" (ID 0123456789abcdef0123456789abcdef).\n\n${REVIEW_PLAN_FIRST}`,
    );
  });
});
