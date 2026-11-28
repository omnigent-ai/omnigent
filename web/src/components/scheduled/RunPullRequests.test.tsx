import { describe, expect, it } from "vitest";
import { runPullRequestsSummary } from "@/components/scheduled/RunPullRequests";
import type { ScheduledTaskRunPullRequest } from "@/lib/scheduledTasksApi";

function pr(
  number: number,
  overrides: Partial<ScheduledTaskRunPullRequest> = {},
): ScheduledTaskRunPullRequest {
  return {
    url: `https://github.com/example/repo/pull/${number}`,
    provider: "github",
    repository: "example/repo",
    number,
    relationship: "created",
    state: "OPEN",
    ...overrides,
  };
}

describe("runPullRequestsSummary", () => {
  it("names a single PR with its state, or alone when its state is unknown", () => {
    expect(runPullRequestsSummary([pr(212, { state: "MERGED" })])).toBe("PR #212 merged");
    expect(runPullRequestsSummary([pr(7, { state: "OPEN", is_draft: true })])).toBe("PR #7 draft");
    expect(runPullRequestsSummary([pr(9, { state: null })])).toBe("PR #9");
  });

  it("counts several PRs per state, in a fixed order", () => {
    expect(
      runPullRequestsSummary([
        pr(1, { state: "CLOSED" }),
        pr(2, { state: "MERGED" }),
        pr(3, { state: "MERGED" }),
        pr(4, { state: null }),
        pr(5),
      ]),
    ).toBe("PRs: 1 open, 2 merged, 1 closed, 1 unknown");
  });

  it("uses the number prefix a provider plugin reports", () => {
    const request = pr(5, {
      provider: "forge",
      provider_display: {
        id: "forge",
        display_name: "Forge",
        request_name: "change request",
        number_prefix: "!",
      },
      state: "MERGED",
    });
    expect(runPullRequestsSummary([request])).toBe("PR !5 merged");
  });
});
