import { describe, expect, it } from "vitest";
import type {
  PullRequestCheckRun,
  PullRequestChecks,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  completedChecksLabel,
  orderCheckRuns,
  summarizeChecks,
  unlistedChecksLabel,
} from "./pullRequestCheckSummary";

function run(name: string, bucket: PullRequestCheckRun["bucket"]): PullRequestCheckRun {
  return { name, bucket, url: null };
}

function checks(runs: PullRequestCheckRun[], extra: Partial<PullRequestChecks> = {}) {
  const count = (bucket: PullRequestCheckRun["bucket"]) =>
    runs.filter((entry) => entry.bucket === bucket).length;
  return {
    passing: count("passing"),
    failing: count("failing"),
    pending: count("pending"),
    cancelled: count("cancelled"),
    skipped: count("skipped"),
    total: runs.length,
    runs,
    ...extra,
  };
}

const summary = (input: PullRequestChecks) => summarizeChecks(input, "GitHub");

describe("orderCheckRuns", () => {
  it("lists failures, then running, cancelled, skipped, and passing jobs, each by name", () => {
    const ordered = orderCheckRuns([
      run("lint", "passing"),
      run("e2e 10", "failing"),
      run("docs", "skipped"),
      run("e2e 2", "failing"),
      run("deploy", "pending"),
      run("Build", "cancelled"),
      run("", "failing"),
    ]);
    expect(ordered.map((entry) => entry.name)).toEqual([
      "e2e 2",
      "e2e 10",
      "deploy",
      "Build",
      "docs",
      "lint",
    ]);
  });
});

describe("summarizeChecks", () => {
  it("leads with failures and keeps running jobs beside them", () => {
    const result = summary(
      checks([run("e2e", "failing"), run("unit", "pending"), run("lint", "passing")]),
    );
    expect(result).toMatchObject({
      tone: "failing",
      title: "1 failing check",
      detail: "1 failing, 1 running, 1 successful",
      completedOpen: false,
    });
    expect(result.attention.map((entry) => entry.name)).toEqual(["e2e", "unit"]);
    expect(result.completed.map((entry) => entry.name)).toEqual(["lint"]);
  });

  it("counts several failures in the headline", () => {
    expect(summary(checks([run("a", "failing"), run("b", "failing")])).title).toBe(
      "2 failing checks",
    );
  });

  it("says checks are running when nothing failed yet", () => {
    expect(summary(checks([run("unit", "pending"), run("lint", "passing")]))).toMatchObject({
      tone: "pending",
      title: "Checks are still running",
    });
  });

  it("says all checks passed only when every job passed or was skipped", () => {
    expect(summary(checks([run("unit", "passing"), run("docs", "skipped")]))).toMatchObject({
      tone: "passing",
      title: "All checks have passed",
      detail: "1 successful, 1 skipped",
      completedOpen: true,
    });
  });

  it("names cancelled and all-skipped runs instead of calling them passed", () => {
    expect(summary(checks([run("unit", "passing"), run("e2e", "cancelled")]))).toMatchObject({
      tone: "cancelled",
      title: "Checks were cancelled",
    });
    expect(summary(checks([run("docs", "skipped"), run("e2e", "skipped")]))).toMatchObject({
      tone: "skipped",
      title: "Checks were skipped",
      detail: "2 skipped",
    });
  });

  it("never claims a pass when some results couldn't be read", () => {
    expect(summary(checks([run("unit", "passing")], { partial: true }))).toMatchObject({
      tone: "none",
      title: "Some checks couldn’t be loaded",
      detail: "1 successful",
    });
    // Counts that don't add up to the total mean some results are missing too.
    expect(summary(checks([run("unit", "passing")], { total: 3 })).title).toBe(
      "Some checks couldn’t be loaded",
    );
    // A failure still leads, with the gap noted.
    expect(summary(checks([run("e2e", "failing")], { partial: true }))).toMatchObject({
      title: "1 failing check",
      detail: "1 failing · some checks couldn’t be loaded",
    });
  });

  it("tells no checks apart from checks that couldn't be loaded", () => {
    expect(summary(checks([]))).toMatchObject({
      tone: "none",
      title: "No checks reported",
      detail: "GitHub has no CI results for this commit.",
    });
    expect(summary(checks([], { partial: true })).title).toBe("Checks couldn’t be loaded");
  });

  it("unfolds the completed checks only when nothing needs attention and the list is short", () => {
    const quiet = (count: number) =>
      Array.from({ length: count }, (_, index) => run(`job ${index}`, "passing"));
    expect(summary(checks(quiet(5))).completedOpen).toBe(true);
    expect(summary(checks(quiet(6))).completedOpen).toBe(false);
    expect(summary(checks([...quiet(1), run("unit", "pending")])).completedOpen).toBe(false);
  });
});

describe("checks the host counted but didn't list", () => {
  const passingRuns = (count: number) =>
    Array.from({ length: count }, (_, index) => run(`job ${index}`, "passing"));

  it("flags a failure beyond the listed jobs without doubting the exact counts", () => {
    // The host lists at most 300 jobs; the counts still cover all of them.
    const result = summary({
      passing: 300,
      failing: 1,
      pending: 0,
      total: 301,
      runs: passingRuns(300),
    });
    expect(result).toMatchObject({
      tone: "failing",
      title: "1 failing check",
      detail: "1 failing, 300 successful",
      attention: [],
      unlistedAttention: 1,
      unlistedCompleted: 0,
      completedOpen: false,
    });
  });

  it("keeps an all-pass result when only the job list is capped", () => {
    expect(
      summary({ passing: 350, failing: 0, pending: 0, total: 350, runs: passingRuns(300) }),
    ).toMatchObject({ tone: "passing", unlistedAttention: 0, unlistedCompleted: 50 });
  });

  it("counts every job as unlisted when the host sent no job details", () => {
    expect(summary({ passing: 3, failing: 2, pending: 1, total: 6, runs: [] })).toMatchObject({
      title: "2 failing checks",
      unlistedAttention: 3,
      unlistedCompleted: 3,
    });
  });

  it("says which jobs are missing, failures first", () => {
    expect(unlistedChecksLabel(1, 40)).toBe("1 failing or running check isn’t listed here.");
    expect(unlistedChecksLabel(2, 0)).toBe("2 failing or running checks aren’t listed here.");
    expect(unlistedChecksLabel(0, 1)).toBe("1 completed check isn’t listed here.");
    expect(unlistedChecksLabel(0, 50)).toBe("50 completed checks aren’t listed here.");
  });
});

describe("completedChecksLabel", () => {
  it("pluralizes", () => {
    expect(completedChecksLabel(1)).toBe("1 completed check");
    expect(completedChecksLabel(36)).toBe("36 completed checks");
  });
});
