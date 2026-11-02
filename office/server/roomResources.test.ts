import { describe, expect, it } from "vitest";
import { UsageTracker, sessionUsage, workspacePath } from "./roomResources.ts";

describe("sessionUsage", () => {
  it("adds tokens and cost across models and tolerates missing usage", () => {
    expect(
      sessionUsage({
        usage_by_model: {
          "gpt-6.1-sol": { total_tokens: 900, total_cost_usd: 0.2 },
          opus: { total_tokens: 100, total_cost_usd: 0.05 },
        },
      }),
    ).toEqual({ tokens: 1000, costUsd: 0.25 });
    expect(sessionUsage(null)).toEqual({ tokens: 0, costUsd: 0 });
  });
});

describe("UsageTracker", () => {
  it("sums a project and re-reads a session only after it changes", async () => {
    const reads: string[] = [];
    const tracker = new UsageTracker(async (id) => {
      reads.push(id);
      return { usage_by_model: { m: { total_tokens: id === "a" ? 10 : 5, total_cost_usd: 0 } } };
    });
    expect(await tracker.projectUsage([{ id: "a", updated_at: 1 }, { id: "b", updated_at: 1 }])).toEqual({ totalTokens: 15, costUsd: 0, sessions: 2 });
    await tracker.projectUsage([{ id: "a", updated_at: 1 }, { id: "b", updated_at: 2 }]);
    expect(reads).toEqual(["a", "b", "b"]);
  });

  it("re-reads a running session each time so its usage climbs during a turn", async () => {
    let tokens = 0;
    const tracker = new UsageTracker(async () => ({ usage_by_model: { m: { total_tokens: (tokens += 10) } } }));
    const running = [{ id: "a", updated_at: 1, status: "running" }];
    expect((await tracker.projectUsage(running)).totalTokens).toBe(10);
    expect((await tracker.projectUsage(running)).totalTokens).toBe(20);
    expect((await tracker.projectUsage([{ id: "a", updated_at: 1, status: "idle" }])).totalTokens).toBe(30);
    expect((await tracker.projectUsage([{ id: "a", updated_at: 1, status: "idle" }])).totalTokens).toBe(30);
  });
});

it("retries a session whose read failed instead of counting it as zero for good", async () => {
  let fail = true;
  const tracker = new UsageTracker(async () => {
    if (fail) throw new Error("timeout");
    return { usage_by_model: { m: { total_tokens: 7 } } };
  });
  expect((await tracker.projectUsage([{ id: "a", updated_at: 1 }])).totalTokens).toBe(0);
  fail = false;
  expect((await tracker.projectUsage([{ id: "a", updated_at: 1 }])).totalTokens).toBe(7);
});

describe("workspacePath", () => {
  it("resolves paths inside the workspace", () => {
    expect(workspacePath("/repo/", "")).toEqual({ absolute: "/repo", relative: "" });
    expect(workspacePath("/repo", "src/app.ts")).toEqual({ absolute: "/repo/src/app.ts", relative: "src/app.ts" });
    expect(workspacePath("/repo", "/src/./lib/")).toEqual({ absolute: "/repo/src/lib", relative: "src/lib" });
  });

  it("refuses paths that leave the workspace", () => {
    expect(workspacePath("/repo", "../etc/passwd")).toBeNull();
    expect(workspacePath("/repo", "src/../../etc")).toBeNull();
  });
});
