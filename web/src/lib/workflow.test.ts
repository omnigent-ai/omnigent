import { describe, expect, it } from "vitest";
import {
  emptyTriggerFields,
  encodeTriggerPrompt,
  stripTriggerHeader,
} from "@/components/scheduled/triggerTemplates";

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
  stopPlanMessage,
  taskActionMessage,
  workflowMessage,
} from "./workflow";

describe("continuePlanMessage", () => {
  it("asks the agent to pick the plan back up, by name when it has one", () => {
    expect(continuePlanMessage("Triage flaky tests")).toContain(
      'Continue the plan "Triage flaky tests" from where you left off, and work through the tasks that are left.',
    );
    expect(continuePlanMessage(null)).toContain(
      "Continue the plan from where you left off, and work through the tasks that are left.",
    );
  });

  it("restores stopped workers and their descendants without duplicating existing work", () => {
    const message = continuePlanMessage(null);
    expect(message).toContain("Resume or restart");
    expect(message).toContain("background tasks, subagents, and other Omnigent sessions");
    expect(message).toContain("recursively");
    expect(message).toContain("reuse work that is still running");
    expect(message).toContain("preserve completed results");
    expect(message).toContain("do not duplicate work");
    expect(message).toContain("usable cancellation handle");
  });
});

describe("stopPlanMessage", () => {
  it.each(["Triage flaky tests", null])(
    "asks the main agent to stop the entire worker tree: %s",
    (name) => {
      const message = stopPlanMessage(name);
      expect(message).toContain(name ? `Stop the plan "${name}" now.` : "Stop the plan now.");
      expect(message).toContain("background tasks, subagents, and other Omnigent sessions");
      expect(message).toContain("this workflow run");
      expect(message).toContain(
        "its own background tasks, agents, and Omnigent sessions recursively",
      );
      expect(message).toContain("verify that the work has stopped");
      expect(message).toContain("original task/terminal handles");
      expect(message).toContain("wait for their exit results");
      expect(message).toContain("empty process list in another sandbox is not proof");
      expect(message).toContain("report anything you could not stop");
      expect(message).toContain("record the worker/session IDs");
      expect(message).toContain("not done or failed");
      expect(message).toContain("until I explicitly tell you to continue");
    },
  );
});

describe("taskActionMessage", () => {
  it("asks to run, continue or retry the task by name, then the rest of the plan", () => {
    expect(taskActionMessage("run", "Share the files")).toContain(
      'Run the task "Share the files", then go on with the rest of the plan.',
    );
    expect(taskActionMessage("continue", "Review #6109")).toContain(
      'Continue the task "Review #6109", then go on with the rest of the plan.',
    );
    expect(taskActionMessage("retry", "Run the tests")).toContain(
      'Retry the task "Run the tests", then go on with the rest of the plan.',
    );
  });

  it("tells the agent it may go ahead with a task waiting for the user", () => {
    expect(taskActionMessage("proceed", "Approve the deploy")).toContain(
      `It's fine to continue the task "Approve the deploy". Mark it started and go ahead with it (or mark it done if its work is already done), then go on with the rest of the plan.`,
    );
  });

  it.each(["run", "continue", "retry", "proceed"] as const)(
    "%s restores the workers needed for the remaining work",
    (action) => {
      const message = taskActionMessage(action, "Review changes");
      expect(message).toContain("Resume or restart");
      expect(message).toContain("Omnigent sessions");
      expect(message).toContain("recursively");
      expect(message).toContain("do not duplicate work");
    },
  );
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

  it.each(["new", "continue"] as const)(
    "reads trigger instructions in %s sessions without trusting stale headers",
    (taskSessionMode) => {
      const encoded = encodeTriggerPrompt(
        {
          ...emptyTriggerFields("github"),
          source: "github",
          scope: "example/project",
          instructions: savedWorkflowMessage("/", workflow, "Only urgent issues", false),
          taskSessionMode,
          worker: {
            placement: "same_host",
            agentId: null,
            model: "",
            effort: "",
            permissionMode: "",
          },
        },
        { sessionLink: "https://example.test/c/CONVERSATION_ID" },
      );
      const body = stripTriggerHeader(encoded);
      const header = encoded.slice(0, encoded.length - body.length);
      for (const prompt of [encoded, body]) {
        expect(parseSavedWorkflowMessage(prompt)).toEqual({
          ...workflow,
          details: "Only urgent issues",
        });
      }
      expect(parseSavedWorkflowMessage(header + body.replace(workflow.id, "b".repeat(32)))).toEqual(
        {
          ...workflow,
          id: "b".repeat(32),
          details: "Only urgent issues",
        },
      );
      expect(parseSavedWorkflowMessage(header + "Summarize overnight activity")).toBeNull();
    },
  );
});

describe("runSavedWorkflowPrompt", () => {
  const workflow = { id: "0123456789abcdef0123456789abcdef", name: "Triage flaky tests" };

  it("asks the workflow skill to run the saved workflow by ID", () => {
    expect(runSavedWorkflowPrompt(workflow)).toBe(
      'Use the workflow skill to run the saved workflow "Triage flaky tests" (ID 0123456789abcdef0123456789abcdef).\n\nIf the session journal already contains this workflow\'s plan, continue that run without writing a second starting plan.',
    );
    expect(runSavedWorkflowPrompt(workflow, "  \n")).toBe(runSavedWorkflowPrompt(workflow));
  });

  it("adds the run's additional instructions as their own paragraph", () => {
    expect(runSavedWorkflowPrompt(workflow, "  Only the upload tests.\n")).toBe(
      `${runSavedWorkflowPrompt(workflow)}\n\nOnly the upload tests.`,
    );
  });
});
