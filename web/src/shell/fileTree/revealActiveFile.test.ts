import { describe, expect, it } from "vitest";
import {
  HIDDEN_FILE_REASON,
  NO_ACTIVE_FILE_REASON,
  OUTSIDE_WORKSPACE_REASON,
  planActiveFileReveal,
} from "./revealActiveFile";

const plan = (
  activePath: string | null,
  location = "",
  { workspaceRoot = "/work", showHidden = false } = {},
) => planActiveFileReveal({ activePath, location, workspaceRoot, showHidden });

describe("planActiveFileReveal", () => {
  it("waits for an open file", () => {
    expect(plan(null)).toEqual({
      kind: "unavailable",
      availability: { available: false, reason: NO_ACTIVE_FILE_REASON },
    });
    expect(plan("")).toMatchObject({ kind: "unavailable" });
  });

  it("reveals a file in place when the browsed folder holds it", () => {
    expect(plan("experimental/iulian.dragos/OWNERS")).toEqual({
      kind: "reveal",
      rerootToWorkspace: false,
      availability: { available: true },
    });
    expect(plan("/work/experimental/OWNERS", "experimental")).toMatchObject({
      rerootToWorkspace: false,
    });
  });

  it("returns to the workspace for a file the browsed folder doesn't hold", () => {
    expect(plan("docs/guide.md", "experimental/d0000")).toMatchObject({
      kind: "reveal",
      rerootToWorkspace: true,
    });
    expect(plan("docs/guide.md", "/tmp/elsewhere")).toMatchObject({ rerootToWorkspace: true });
  });

  it("names why a file outside the workspace can't be revealed", () => {
    expect(plan("/etc/hosts")).toEqual({
      kind: "unavailable",
      availability: { available: false, reason: OUTSIDE_WORKSPACE_REASON },
    });
  });

  it("reveals a file outside the workspace from a browsed folder that holds it", () => {
    expect(plan("/tmp/notes/a.md", "/tmp")).toMatchObject({
      kind: "reveal",
      rerootToWorkspace: false,
    });
  });

  it("offers a hidden file only while hidden files show", () => {
    expect(plan(".github/workflows/ci.yml")).toEqual({
      kind: "unavailable",
      availability: { available: false, reason: HIDDEN_FILE_REASON },
    });
    expect(plan("src/.env")).toMatchObject({ kind: "unavailable" });
    expect(plan(".github/workflows/ci.yml", "", { showHidden: true })).toMatchObject({
      kind: "reveal",
    });
  });

  it("judges hiddenness inside the browsed folder, even one that is itself hidden", () => {
    expect(plan(".github/workflows/ci.yml", ".github")).toMatchObject({ kind: "reveal" });
  });
});
