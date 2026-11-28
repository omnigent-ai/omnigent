import { describe, expect, it, vi } from "vitest";

import { latestChangeStatus } from "./changeStatus";

type Snapshot = Parameters<typeof latestChangeStatus<{ pr: string }>>[0][number];

const snapshot = (
  id: number,
  status: Snapshot["status"],
  submittedAt: number,
  pr = "a",
): Snapshot => ({
  id,
  status,
  submittedAt,
  variables: { pr },
  error: status === "error" ? new Error(`failed ${id}`) : null,
});

describe("latestChangeStatus", () => {
  const all = () => true;

  it("lets the latest change decide: a fresh success clears an earlier failure", () => {
    const dismiss = vi.fn();
    expect(latestChangeStatus([snapshot(1, "error", 10)], all, dismiss).kind).toBe("failed");
    expect(
      latestChangeStatus([snapshot(1, "error", 10), snapshot(2, "pending", 20)], all, dismiss).kind,
    ).toBe("pending");
    expect(
      latestChangeStatus([snapshot(1, "error", 10), snapshot(2, "success", 20)], all, dismiss),
    ).toEqual({ kind: "idle" });
  });

  it("breaks a same-millisecond tie by the cache's order, whatever order it lists them in", () => {
    const status = latestChangeStatus(
      [snapshot(3, "error", 10), snapshot(2, "success", 10)],
      all,
      vi.fn(),
    );
    expect(status.kind).toBe("failed");
  });

  it("reads only the changes `matches` takes, and dismisses the one it shows", () => {
    const dismiss = vi.fn();
    const status = latestChangeStatus(
      [snapshot(1, "error", 10, "a"), snapshot(2, "pending", 20, "b")],
      (variables) => variables.pr === "a",
      dismiss,
    );
    expect(status.kind).toBe("failed");
    if (status.kind === "failed") status.dismiss();
    expect(dismiss).toHaveBeenCalledWith([1]);
  });
});
