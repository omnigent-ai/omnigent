import { describe, expect, it, vi } from "vitest";
import type {
  ExtensionContext,
  ExtensionSessionPage,
  ExtensionSessionSummary,
} from "@omnigent/extension-sdk";
import { loadProjects, loadSessions } from "./sessionData";

function context(capabilities: string[]): ExtensionContext {
  return {
    capabilities,
    sessions: {
      getCached: vi.fn(async () => null),
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

function session(id: string, title = id): ExtensionSessionSummary {
  return {
    id,
    title,
    status: "idle",
    unread: false,
    titleProvisional: false,
    workspace: null,
    gitBranch: null,
    projectId: null,
    createdAt: 1,
    updatedAt: 1,
  };
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

  it("previews a sparse cache then merges canonical pages from the beginning", async () => {
    const extensionContext = context([
      "sessions.listPage",
      "sessions.getCached",
    ]);
    const cached = [
      session("d"),
      session("a", "Stale title"),
      session("removed"),
      session("d"),
    ];
    vi.mocked(extensionContext.sessions.getCached).mockResolvedValue(cached);
    vi.mocked(extensionContext.sessions.listPage)
      .mockResolvedValueOnce({
        sessions: [session("a", "Current title"), session("b")],
        nextCursor: "b",
        hasMore: true,
      })
      .mockResolvedValueOnce({
        sessions: [session("b", "Updated title"), session("c"), session("d")],
        nextCursor: null,
        hasMore: false,
      });
    const progress = vi.fn();

    await expect(loadSessions(extensionContext, progress)).resolves.toEqual([
      session("a", "Current title"),
      session("b", "Updated title"),
      session("c"),
      session("d"),
    ]);
    expect(extensionContext.sessions.getCached).toHaveBeenCalledWith({
      limit: 25,
    });
    expect(extensionContext.sessions.listPage).toHaveBeenNthCalledWith(1, {
      after: null,
      limit: 1_000,
    });
    expect(extensionContext.sessions.listPage).toHaveBeenNthCalledWith(2, {
      after: "b",
      limit: 1_000,
    });
    expect(
      progress.mock.calls.map(([value]) => ({
        ids: value.sessions.map((item: ExtensionSessionSummary) => item.id),
        hasMore: value.hasMore,
      })),
    ).toEqual([
      { ids: ["d", "a", "removed"], hasMore: true },
      { ids: ["d", "a", "removed", "b"], hasMore: true },
      { ids: ["a", "b", "c", "d"], hasMore: false },
    ]);
    expect(
      progress.mock.calls[1][0].sessions.find(
        (item: ExtensionSessionSummary) => item.id === "a",
      ).title,
    ).toBe("Current title");
  });

  it("reports the preview before awaiting a canonical request and retains it on failure", async () => {
    const extensionContext = context([
      "sessions.listPage",
      "sessions.getCached",
    ]);
    vi.mocked(extensionContext.sessions.getCached).mockResolvedValue([
      session("cached"),
    ]);
    vi.mocked(extensionContext.sessions.listPage).mockRejectedValue(
      new Error("offline"),
    );
    const progress = vi.fn(() => {
      expect(extensionContext.sessions.listPage).not.toHaveBeenCalled();
    });

    await expect(loadSessions(extensionContext, progress)).rejects.toThrow(
      "offline",
    );
    expect(progress).toHaveBeenCalledExactlyOnceWith({
      sessions: [session("cached")],
      hasMore: true,
    });
  });

  it("removes stale preview-only rows when the canonical list is empty", async () => {
    const extensionContext = context([
      "sessions.listPage",
      "sessions.getCached",
    ]);
    vi.mocked(extensionContext.sessions.getCached).mockResolvedValue([
      session("removed"),
    ]);
    const progress = vi.fn();
    await expect(loadSessions(extensionContext, progress)).resolves.toEqual([]);
    expect(progress).toHaveBeenLastCalledWith({ sessions: [], hasMore: false });
  });

  it.each([null, []])(
    "uses a small first page when the preview is %j",
    async (cached) => {
      const extensionContext = context([
        "sessions.listPage",
        "sessions.getCached",
      ]);
      vi.mocked(extensionContext.sessions.getCached).mockResolvedValue(cached);
      await expect(loadSessions(extensionContext)).resolves.toEqual([]);
      expect(
        extensionContext.sessions.listPage,
      ).toHaveBeenCalledExactlyOnceWith({ after: null, limit: 25 });
    },
  );

  it("falls back to canonical pages if the optional cache read fails", async () => {
    const extensionContext = context([
      "sessions.listPage",
      "sessions.getCached",
    ]);
    vi.mocked(extensionContext.sessions.getCached).mockRejectedValue(
      new Error("unavailable"),
    );
    await expect(loadSessions(extensionContext)).resolves.toEqual([]);
    expect(extensionContext.sessions.listPage).toHaveBeenCalledExactlyOnceWith({
      after: null,
      limit: 25,
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
