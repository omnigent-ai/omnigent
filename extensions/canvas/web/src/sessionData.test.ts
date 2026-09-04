import { describe, expect, it, vi } from "vitest";
import type { ExtensionContext } from "@omnigent/extension-sdk";
import { loadProjects, loadSessions } from "./sessionData";

function context(capabilities: string[]): ExtensionContext {
  return {
    capabilities,
    sessions: { listAll: vi.fn(async () => []) },
    projects: {
      list: vi.fn(async () => [{ id: "p1", name: "Alpha", icon: null }]),
    },
  } as unknown as ExtensionContext;
}

describe("loadSessions", () => {
  it("requires the sessions capability", async () => {
    await expect(loadSessions(context([]))).rejects.toThrow("sessions.read");
  });

  it("loads all bounded pages through the SDK", async () => {
    const extensionContext = context(["sessions.listPage"]);
    await expect(loadSessions(extensionContext)).resolves.toEqual([]);
    expect(extensionContext.sessions.listAll).toHaveBeenCalledWith({
      pageLimit: 25,
    });
  });
});

describe("loadProjects", () => {
  it("is empty without the projects capability", async () => {
    const extensionContext = context(["sessions.listPage"]);
    await expect(loadProjects(extensionContext)).resolves.toEqual([]);
    expect(extensionContext.projects.list).not.toHaveBeenCalled();
  });

  it("lists projects through the SDK when granted", async () => {
    await expect(loadProjects(context(["projects.list"]))).resolves.toEqual([
      { id: "p1", name: "Alpha", icon: null },
    ]);
  });
});
