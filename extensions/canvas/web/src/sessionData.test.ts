import { describe, expect, it, vi } from "vitest";
import type {
  ExtensionContext,
  ExtensionSessionPage,
} from "@omnigent/extension-sdk";
import { loadProjects, loadSessions } from "./sessionData";

function context(capabilities: string[]): ExtensionContext {
  return {
    capabilities,
    sessions: {
      listPage: vi.fn(async (): Promise<ExtensionSessionPage> => ({
        sessions: [],
        nextCursor: null,
        hasMore: false,
      })),
    },
    projects: {
      list: vi.fn(async () => [{ id: "p1", name: "Alpha", icon: null }]),
    },
  } as unknown as ExtensionContext;
}

describe("loadSessions", () => {
  it("requires the sessions capability", async () => {
    await expect(loadSessions(context([]))).rejects.toThrow("sessions.read");
  });

  it("reports each bounded page as it arrives", async () => {
    const extensionContext = context(["sessions.listPage"]);
    const first = {
      id: "s1",
      title: "One",
      status: "idle" as const,
      unread: false,
      titleProvisional: false,
      workspace: null,
      gitBranch: null,
      projectId: null,
      createdAt: 1,
      updatedAt: 1,
    };
    const second = { ...first, id: "s2", title: "Two" };
    vi.mocked(extensionContext.sessions.listPage)
      .mockResolvedValueOnce({
        sessions: [first],
        nextCursor: "next",
        hasMore: true,
      })
      .mockResolvedValueOnce({
        sessions: [second],
        nextCursor: null,
        hasMore: false,
      });
    const progress = vi.fn();

    await expect(loadSessions(extensionContext, progress)).resolves.toEqual([
      first,
      second,
    ]);
    expect(extensionContext.sessions.listPage).toHaveBeenNthCalledWith(1, {
      after: null,
      limit: 25,
    });
    expect(extensionContext.sessions.listPage).toHaveBeenNthCalledWith(2, {
      after: "next",
      limit: 1_000,
    });
    expect(progress).toHaveBeenNthCalledWith(1, {
      sessions: [first],
      hasMore: true,
    });
    expect(progress).toHaveBeenNthCalledWith(2, {
      sessions: [first, second],
      hasMore: false,
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
