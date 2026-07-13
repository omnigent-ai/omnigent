import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { parseWorkflowResult, WorkflowResultCard } from "./WorkflowResultCard";

afterEach(cleanup);

const SAMPLE = `Here is the outcome:
<workflow_result>
{
  "summary": "Implemented telemetry opt-out handling.",
  "pr_url": "https://github.com/omnigent-ai/omnigent/pull/2499",
  "branch": "polly/fix-2487-telemetry",
  "files_changed": ["README.md", "config.yaml"],
  "gates": { "precommit": "pass — ran hooks", "tests": "fail — 2 broke" }
}
</workflow_result>
Done.`;

describe("parseWorkflowResult", () => {
  it("splits prose from the result block and parses the JSON", () => {
    const parsed = parseWorkflowResult(SAMPLE);
    expect(parsed).not.toBeNull();
    expect(parsed!.before).toBe("Here is the outcome:");
    expect(parsed!.after).toBe("Done.");
    expect(parsed!.result.branch).toBe("polly/fix-2487-telemetry");
  });

  it("returns null for text without a result block", () => {
    expect(parseWorkflowResult("just a normal message")).toBeNull();
  });

  it("returns null when the block JSON is malformed (falls back to markdown)", () => {
    expect(parseWorkflowResult("<workflow_result>{not json}</workflow_result>")).toBeNull();
  });

  it("uses the last block when several are present", () => {
    const text =
      "<workflow_result>{\"summary\": \"first\"}</workflow_result>" +
      "<workflow_result>{\"summary\": \"second\"}</workflow_result>";
    expect(parseWorkflowResult(text)!.result.summary).toBe("second");
  });
});

describe("WorkflowResultCard", () => {
  it("renders summary, PR link, branch, gates, and files", () => {
    const parsed = parseWorkflowResult(SAMPLE)!;
    render(<WorkflowResultCard result={parsed.result} raw={parsed.raw} />);

    expect(screen.getByText("Implemented telemetry opt-out handling.")).toBeInTheDocument();

    const link = screen.getByRole("link", { name: /omnigent-ai\/omnigent\/pull\/2499/ });
    expect(link).toHaveAttribute("href", "https://github.com/omnigent-ai/omnigent/pull/2499");
    expect(link).toHaveAttribute("target", "_blank");

    expect(screen.getByText("polly/fix-2487-telemetry")).toBeInTheDocument();

    // Gates render with pass/fail styling; both names appear.
    expect(screen.getByText("precommit")).toBeInTheDocument();
    expect(screen.getByText("tests")).toBeInTheDocument();

    expect(screen.getByText("2 files changed")).toBeInTheDocument();
  });
});
