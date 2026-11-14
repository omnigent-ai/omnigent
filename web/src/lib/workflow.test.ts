import { describe, expect, it } from "vitest";

import {
  REVIEW_PLAN_FIRST,
  continuePlanMessage,
  createWorkflowPrompt,
  editWorkflowPlanPrompt,
  runSavedWorkflowPrompt,
  offersSaveWorkflow,
  offersWorkflow,
  parseSavedWorkflowMessage,
  savedWorkflowMessage,
  saveWorkflowMessage,
  taskActionMessage,
  workflowMessage,
} from "./workflow";

describe("continuePlanMessage", () => {
  it("asks the agent to pick the plan back up, by name when it has one", () => {
    expect(continuePlanMessage("Triage flaky tests")).toBe(
      'Continue the plan "Triage flaky tests" from where you left off, and work through the tasks that are left.',
    );
    expect(continuePlanMessage(null)).toBe(
      "Continue the plan from where you left off, and work through the tasks that are left.",
    );
  });
});

describe("taskActionMessage", () => {
  it("asks to run, continue or retry the task by name, then the rest of the plan", () => {
    expect(taskActionMessage("run", "Share the files")).toBe(
      'Run the task "Share the files", then go on with the rest of the plan.',
    );
    expect(taskActionMessage("continue", "Review #6109")).toBe(
      'Continue the task "Review #6109", then go on with the rest of the plan.',
    );
    expect(taskActionMessage("retry", "Run the tests")).toBe(
      'Retry the task "Run the tests", then go on with the rest of the plan.',
    );
  });

  it("tells the agent it may go ahead with a task waiting for the user", () => {
    expect(taskActionMessage("proceed", "Approve the deploy")).toBe(
      `It's fine to continue the task "Approve the deploy". Mark it started and go ahead with it (or mark it done if its work is already done), then go on with the rest of the plan.`,
    );
  });
});

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

describe("editWorkflowPlanPrompt", () => {
  const workflow = {
    id: "0123456789abcdef0123456789abcdef",
    name: "Triage flaky tests",
    description: "Rerun the suite and file issues",
    steps: [{ id: "rerun", name: "Rerun the suite", goal: null, depends_on: [] }],
  };

  it("asks to load a saved plan and wait for requested edits before proposing changes", () => {
    const prompt = editWorkflowPlanPrompt(workflow);
    expect(prompt).toContain(`Help me edit the reusable workflow "${workflow.name}"`);
    expect(prompt).toContain(`(ID ${workflow.id})`);
    expect(prompt).toContain("Load the existing workflow with sys_workflow_load");
    expect(prompt).toContain("Read the saved plan and use it as the starting point");
    expect(prompt).toContain("wait for my answer before proposing or publishing changes");
    expect(prompt).toContain("Preserve steps I haven't asked to change");
    expect(prompt).not.toContain("Draft a useful first proposal now");
  });

  it("drafts a first proposal only when the workflow has no saved steps", () => {
    const prompt = editWorkflowPlanPrompt({ ...workflow, steps: [] });
    expect(prompt).toContain(`Help me design the reusable workflow "${workflow.name}"`);
    expect(prompt).toContain(`Workflow description: ${workflow.description}`);
    expect(prompt).toContain("Draft a useful first proposal now");
    expect(prompt).toContain("I will use Save plan on the workflow page to apply the proposal");
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
  const workflow = { id: "0123456789abcdef0123456789abcdef", name: "Triage flaky tests" };

  it("asks the workflow skill to run the saved workflow by ID", () => {
    expect(runSavedWorkflowPrompt(workflow)).toBe(
      'Use the workflow skill to run the saved workflow "Triage flaky tests" (ID 0123456789abcdef0123456789abcdef).',
    );
    expect(runSavedWorkflowPrompt(workflow, "  \n")).toBe(runSavedWorkflowPrompt(workflow));
  });

  it("adds the run's additional instructions as their own paragraph", () => {
    expect(runSavedWorkflowPrompt(workflow, "  Only the upload tests.\n")).toBe(
      'Use the workflow skill to run the saved workflow "Triage flaky tests" (ID 0123456789abcdef0123456789abcdef).\n\nOnly the upload tests.',
    );
  });
});
