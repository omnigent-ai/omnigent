import { describe, expect, it } from "vitest";
import { treeChangeStatuses } from "./treeChangeStatus";

const statusOf = (entries: ReturnType<typeof treeChangeStatuses>, path: string) =>
  entries.find((entry) => entry.path === path)?.status;

describe("treeChangeStatuses", () => {
  it("marks each changed file with its own status", () => {
    const entries = treeChangeStatuses([
      { path: "new.ts", status: "created" },
      { path: "old.ts", status: "deleted" },
      { path: "edit.ts", status: "modified" },
    ]);
    expect(entries).toEqual([
      { path: "new.ts", status: "added" },
      { path: "old.ts", status: "deleted" },
      { path: "edit.ts", status: "modified" },
    ]);
  });

  it("marks every folder above a change with the strongest status inside it", () => {
    const entries = treeChangeStatuses([
      { path: "src/lib/gone.ts", status: "deleted" },
      { path: "src/lib/edit.ts", status: "modified" },
      { path: "src/app/new.ts", status: "created" },
      { path: "docs/gone.md", status: "deleted" },
    ]);
    expect(statusOf(entries, "src/lib/")).toBe("modified");
    expect(statusOf(entries, "src/app/")).toBe("added");
    expect(statusOf(entries, "src/")).toBe("added");
    expect(statusOf(entries, "docs/")).toBe("deleted");
  });

  it("keeps the stronger status whatever order the changes arrive in", () => {
    const created = treeChangeStatuses([
      { path: "a/x", status: "created" },
      { path: "a/y", status: "deleted" },
    ]);
    const deleted = treeChangeStatuses([
      { path: "a/y", status: "deleted" },
      { path: "a/x", status: "created" },
    ]);
    expect(statusOf(created, "a/")).toBe("added");
    expect(statusOf(deleted, "a/")).toBe("added");
  });

  it("has nothing to mark when nothing changed", () => {
    expect(treeChangeStatuses([])).toEqual([]);
  });
});
