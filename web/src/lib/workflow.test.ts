import { describe, expect, it } from "vitest";

import { REVIEW_PLAN_FIRST, offersWorkflow, workflowMessage } from "./workflow";

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
