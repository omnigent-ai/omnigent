import { describe, expect, it } from "vitest";

import { normalizeComposerContextState, type ComposerContextState } from "./composerContext";
import {
  composerContextFromCreateSession,
  composerContextFromLabels,
  composerContextFromMetadata,
  composerContextToCreateSession,
  composerContextToLabel,
  composerContextToLabels,
  composerContextToMetadata,
} from "./composerContextAdapters";

const state: ComposerContextState = {
  workingDirectory: { kind: "selected", path: "/repo" },
  worktree: { kind: "new", branchName: "feature/context", baseBranch: "main" },
  repositories: [
    { id: "primary", url: "https://github.com/acme/app.git", branch: "main" },
    { id: "docs", url: "https://github.com/acme/docs.git", branch: null },
  ],
};

describe("composer context", () => {
  it("clears worktree selection only when the working directory is known non-Git", () => {
    expect(normalizeComposerContextState(state, "unknown").worktree.kind).toBe("new");
    expect(normalizeComposerContextState(state, "not_git").worktree).toEqual({ kind: "none" });
  });

  it("preserves first-seen selection identity and order", () => {
    const normalized = normalizeComposerContextState({
      ...state,
      repositories: [state.repositories[1], state.repositories[0], state.repositories[1]],
    });
    expect(normalized.repositories.map(({ id }) => id)).toEqual(["docs", "primary"]);
  });

  it("round-trips persisted metadata including intentional empty selections", () => {
    expect(composerContextFromMetadata(composerContextToMetadata(state))).toEqual(state);

    const empty = normalizeComposerContextState({
      workingDirectory: { kind: "unset" },
      worktree: { kind: "none" },
      repositories: [],
    });
    expect(composerContextFromMetadata(composerContextToMetadata(empty))).toEqual(empty);
  });

  it("round-trips session labels and ignores malformed metadata", () => {
    expect(
      composerContextFromLabels({
        "omnigent.composer_context.v1": composerContextToLabel(state),
      }),
    ).toEqual(state);
    expect(composerContextFromLabels(composerContextToLabels(state))).toEqual(state);
    expect(composerContextFromLabels({ "omnigent.composer_context.v1": "not-json" })).toEqual({
      workingDirectory: { kind: "unset" },
      worktree: { kind: "none" },
      repositories: [],
    });
  });

  it("does not serialize or hydrate legacy MCP context metadata", () => {
    const metadata = composerContextToMetadata(state);
    expect(metadata).not.toHaveProperty("mcp_context");
    expect(
      composerContextFromMetadata({
        ...metadata,
        mcp_context: [{ id: "github", server_name: "github" }],
      } as typeof metadata),
    ).toEqual(state);
  });

  it("uses existing external create-session workspace and git shapes", () => {
    const adapted = composerContextToCreateSession(state, "external");
    expect(adapted.fields).toEqual({
      workspace: "/repo",
      git: { branch_name: "feature/context", base_branch: "main" },
    });
    expect(composerContextFromCreateSession(adapted)).toEqual(state);
  });

  it("uses ordered managed workspaces and retains richer metadata for round trips", () => {
    const adapted = composerContextToCreateSession(state, "managed");
    expect(adapted.fields).toEqual({
      workspaces: ["https://github.com/acme/app.git#main", "https://github.com/acme/docs.git"],
    });
    expect(composerContextFromCreateSession(adapted)).toEqual(state);
  });
});
