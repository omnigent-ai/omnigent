import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useProjectWorkspaces } from "./useProjectWorkspaces";

const PROJECT_KEY = "omnigent:project-workspaces";

describe("useProjectWorkspaces", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it("returns the project's persisted default workspace", () => {
    localStorage.setItem(PROJECT_KEY, JSON.stringify({ Alpha: "/repos/alpha" }));
    const { result } = renderHook(() => useProjectWorkspaces("Alpha"));
    expect(result.current.projectWorkspace).toBe("/repos/alpha");
  });

  it("returns null for a project with no stored default", () => {
    localStorage.setItem(PROJECT_KEY, JSON.stringify({ Alpha: "/repos/alpha" }));
    const { result } = renderHook(() => useProjectWorkspaces("Beta"));
    expect(result.current.projectWorkspace).toBeNull();
  });

  it("returns null for the unfiled (empty) project", () => {
    localStorage.setItem(PROJECT_KEY, JSON.stringify({ Alpha: "/repos/alpha" }));
    const { result } = renderHook(() => useProjectWorkspaces(""));
    expect(result.current.projectWorkspace).toBeNull();
  });

  it("never exposes the previous project's default after a project switch", () => {
    // Synchronous read (not effect-based) so the render right after a project
    // change already reflects the new project — no stale frame that could seed
    // the wrong directory into the composer's prefill.
    localStorage.setItem(
      PROJECT_KEY,
      JSON.stringify({ Alpha: "/repos/alpha", Beta: "/repos/beta" }),
    );
    const renders: { project: string; workspace: string | null }[] = [];
    const { rerender } = renderHook(
      ({ project }) => {
        const { projectWorkspace } = useProjectWorkspaces(project);
        renders.push({ project, workspace: projectWorkspace });
      },
      { initialProps: { project: "Alpha" } },
    );
    rerender({ project: "Beta" });

    const betaRenders = renders.filter((r) => r.project === "Beta");
    expect(betaRenders.length).toBeGreaterThan(0);
    for (const r of betaRenders) {
      expect(r.workspace).toBe("/repos/beta");
    }
  });

  it("setProjectWorkspace persists and reflects the write synchronously", () => {
    const { result } = renderHook(() => useProjectWorkspaces("Alpha"));
    act(() => result.current.setProjectWorkspace("/repos/alpha"));
    expect(result.current.projectWorkspace).toBe("/repos/alpha");
    // A later session in the same project overwrites the default.
    act(() => result.current.setProjectWorkspace("/repos/alpha-v2"));
    expect(result.current.projectWorkspace).toBe("/repos/alpha-v2");
    expect(JSON.parse(localStorage.getItem(PROJECT_KEY)!)).toEqual({ Alpha: "/repos/alpha-v2" });
  });

  it("setProjectWorkspace is a no-op for the unfiled (empty) project", () => {
    const { result } = renderHook(() => useProjectWorkspaces(""));
    act(() => result.current.setProjectWorkspace("/repos/alpha"));
    expect(result.current.projectWorkspace).toBeNull();
    expect(localStorage.getItem(PROJECT_KEY)).toBeNull();
  });

  it("setProjectWorkspace ignores a blank path", () => {
    const { result } = renderHook(() => useProjectWorkspaces("Alpha"));
    act(() => result.current.setProjectWorkspace("   "));
    expect(result.current.projectWorkspace).toBeNull();
    expect(localStorage.getItem(PROJECT_KEY)).toBeNull();
  });
});
