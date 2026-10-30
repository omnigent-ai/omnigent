import type * as TerminalsModule from "@/hooks/useTerminals";
import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  useWorkspaceEnvironment,
  useWorkspaceChangedFiles,
} from "@/hooks/useWorkspaceChangedFiles";
import { useTerminals } from "@/hooks/useTerminals";
import { useWorkspaceResources } from "./useWorkspaceResources";

vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceEnvironment: vi.fn(() => ({ data: { available: true, root: "/workspace" } })),
  useWorkspaceChangedFiles: vi.fn(() => ({ data: { data: [{ path: "notes.md" }] } })),
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof TerminalsModule>()),
  useTerminals: vi.fn(() => ({ terminals: [{ id: "terminal_tui_main" }, { id: "shell" }] })),
}));
afterEach(cleanup);

it.each([
  { permissionLevel: null, shareWorkspaceFiles: false, browse: true },
  { permissionLevel: 2, shareWorkspaceFiles: false, browse: true },
  { permissionLevel: 1, shareWorkspaceFiles: false, browse: false },
  { permissionLevel: 1, shareWorkspaceFiles: true, browse: true },
])("gates file discovery and separates agent and shell terminals: %o", ({ browse, ...access }) => {
  const { result, rerender } = renderHook(
    ({ sessionId, terminalFirst }) =>
      useWorkspaceResources({ ...access, sessionId, terminalFirst, terminalPending: true }),
    { initialProps: { sessionId: "first", terminalFirst: false } },
  );
  expect(useWorkspaceEnvironment).toHaveBeenLastCalledWith("first", { enabled: browse });
  expect(useWorkspaceChangedFiles).toHaveBeenLastCalledWith("first", { enabled: browse });
  expect(result.current.showFilesPanel).toBe(browse);
  expect(result.current.changedCount).toBe(1);
  expect(result.current.isChangedPath("notes.md")).toBe(true);
  expect(result.current.isChangedPath("unknown.md")).toBe(false);
  expect(result.current.openTerminals).toEqual(["terminal:terminal_tui_main", "terminal:shell"]);
  rerender({ sessionId: "second", terminalFirst: true });
  expect(useTerminals).toHaveBeenLastCalledWith("second", { reconcileWhilePending: true });
  expect(result.current.agentTerminal?.id).toBe("terminal_tui_main");
  expect(result.current.openTerminals).toEqual(["terminal:shell"]);
});
