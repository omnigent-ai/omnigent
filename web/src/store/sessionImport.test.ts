import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { markConversationRead } from "@/hooks/useUnseenConversations";
import {
  getSessionSlim,
  importLocalSessions,
  type ImportedSessionRef,
  type LocalImportResult,
} from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import { reloadConversation } from "@/store/chatStore";
import { useSessionImportStore } from "./sessionImport";

vi.mock("@/lib/sessionsApi", () => ({
  importLocalSessions: vi.fn(),
  getSessionSlim: vi.fn(),
}));
vi.mock("@/store/chatStore", () => ({ reloadConversation: vi.fn() }));
vi.mock("@/hooks/useUnseenConversations", () => ({ markConversationRead: vi.fn() }));

const importMock = vi.mocked(importLocalSessions);
const reloadMock = vi.mocked(reloadConversation);
const sessionMock = vi.mocked(getSessionSlim);
const markReadMock = vi.mocked(markConversationRead);

const DONE: LocalImportResult = {
  imported: 2,
  alreadyImported: 0,
  failed: 0,
  sessions: [],
  failures: [],
};

/** A stream the test settles by hand, so the in-flight state can be inspected. */
function pendingImport() {
  let resolve!: (result: LocalImportResult) => void;
  let onSession: ((s: ImportedSessionRef) => void) | undefined;
  importMock.mockImplementation((_host, _source, _limit, cb) => {
    onSession = cb;
    return new Promise<LocalImportResult>((res) => {
      resolve = res;
    });
  });
  return {
    resolve: (result: LocalImportResult) => resolve(result),
    session: (s: ImportedSessionRef) => onSession?.(s),
  };
}

function spyInvalidate(queryClient: QueryClient) {
  return vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);
}

beforeEach(() => {
  importMock.mockReset();
  reloadMock.mockReset().mockResolvedValue(undefined);
  sessionMock.mockReset();
  markReadMock.mockReset();
  useSessionImportStore.setState({
    importing: false,
    importedCount: 0,
    request: null,
    result: null,
    error: null,
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useSessionImportStore", () => {
  it("exposes the in-flight import and the imported count until the stream settles", async () => {
    const stream = pendingImport();
    const queryClient = new QueryClient();
    const invalidate = spyInvalidate(queryClient);

    const run = useSessionImportStore
      .getState()
      .start({ hostId: "host_1", source: "claude", limit: 25 }, queryClient);
    expect(useSessionImportStore.getState().importing).toBe(true);
    expect(importMock).toHaveBeenCalledWith("host_1", "claude", 25, expect.any(Function));

    stream.session({ id: "c1", title: "One" });
    stream.session({ id: "c2", title: null });
    expect(useSessionImportStore.getState().importedCount).toBe(2);
    expect(useSessionImportStore.getState().importing).toBe(true);

    stream.resolve(DONE);
    await run;
    const state = useSessionImportStore.getState();
    expect(state.importing).toBe(false);
    expect(state.result).toEqual(DONE);
    expect(state.error).toBeNull();
    expect(state.importedCount).toBe(2);
    expect(state.request).toEqual({ hostId: "host_1", source: "claude", limit: 25 });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["conversations"] });
  });

  it("keeps the imported count and records the message when the import fails", async () => {
    importMock.mockImplementation(async (_host, _source, _limit, onSession) => {
      onSession?.({ id: "c1", title: "One" });
      throw new Error("Host went offline");
    });

    await useSessionImportStore
      .getState()
      .start({ hostId: "host_1", source: "all", limit: 25 }, new QueryClient());

    const state = useSessionImportStore.getState();
    expect(state.importing).toBe(false);
    expect(state.error).toBe("Host went offline");
    expect(state.result).toBeNull();
    expect(state.importedCount).toBe(1);
  });

  it("passes an exact session id through and ignores a second start while importing", async () => {
    const stream = pendingImport();
    const queryClient = new QueryClient();
    spyInvalidate(queryClient);

    const first = useSessionImportStore
      .getState()
      .start({ hostId: "host_1", source: "codex", limit: 25, sessionId: "thread-9" }, queryClient);
    await useSessionImportStore
      .getState()
      .start({ hostId: "host_1", source: "codex", limit: 25 }, queryClient);

    expect(importMock).toHaveBeenCalledTimes(1);
    expect(importMock).toHaveBeenCalledWith(
      "host_1",
      "codex",
      25,
      expect.any(Function),
      "thread-9",
      false,
    );
    stream.session({ id: "c1", title: "One" });
    expect(reloadMock).not.toHaveBeenCalled();
    stream.resolve(DONE);
    await first;
    expect(sessionMock).not.toHaveBeenCalled();
  });

  it("drops cached copies of each session a forced import replaced and marks it read", async () => {
    reloadMock.mockRejectedValue(new Error("Snapshot fetch failed"));
    sessionMock.mockImplementation(async (id) => {
      if (id === "c1") return { createdAt: 1_700_000_000, updatedAt: 1_700_000_123 } as Session;
      if (id === "c2") return { createdAt: 1_700_000_050 } as Session;
      throw new Error("Session lookup failed");
    });
    const stream = pendingImport();
    const queryClient = new QueryClient();
    const invalidate = spyInvalidate(queryClient);

    const run = useSessionImportStore
      .getState()
      .start(
        { hostId: "host_1", source: "claude", limit: 25, sessionId: "thread-9", force: true },
        queryClient,
      );
    expect(importMock).toHaveBeenCalledWith(
      "host_1",
      "claude",
      25,
      expect.any(Function),
      "thread-9",
      true,
    );

    stream.session({ id: "c1", title: "One" });
    stream.session({ id: "c2", title: "Two" });
    stream.session({ id: "c3", title: "Three" });
    expect(reloadMock).toHaveBeenCalledWith("c1");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["session", "c1"] });
    stream.resolve(DONE);
    await run;
    expect(markReadMock.mock.calls).toEqual([
      ["c1", 1_700_000_123],
      ["c2", 1_700_000_050],
    ]);
    expect(useSessionImportStore.getState().result).toEqual(DONE);
    expect(useSessionImportStore.getState().error).toBeNull();
  });

  it("ignores force when importing recent sessions", async () => {
    importMock.mockImplementation(async (_host, _source, _limit, onSession) => {
      onSession?.({ id: "c1", title: "One" });
      return DONE;
    });
    const queryClient = new QueryClient();
    spyInvalidate(queryClient);

    await useSessionImportStore
      .getState()
      .start({ hostId: "host_1", source: "claude", limit: 25, force: true }, queryClient);

    expect(importMock).toHaveBeenCalledWith("host_1", "claude", 25, expect.any(Function));
    expect(reloadMock).not.toHaveBeenCalled();
    expect(markReadMock).not.toHaveBeenCalled();
  });

  it("refreshes the session list every 5s only while the import is in flight", async () => {
    vi.useFakeTimers();
    const stream = pendingImport();
    const queryClient = new QueryClient();
    const invalidate = spyInvalidate(queryClient);

    const run = useSessionImportStore
      .getState()
      .start({ hostId: "host_1", source: "all", limit: 25 }, queryClient);
    expect(invalidate).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(5000);
    expect(invalidate).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(5000);
    expect(invalidate).toHaveBeenCalledTimes(2);

    stream.resolve(DONE);
    await run;
    // One final refresh when the stream settles, then the cadence stops.
    expect(invalidate).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(invalidate).toHaveBeenCalledTimes(3);
  });
});
