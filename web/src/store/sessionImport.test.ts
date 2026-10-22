import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  importLocalSessions,
  type ImportedSessionRef,
  type LocalImportResult,
} from "@/lib/sessionsApi";
import { useSessionImportStore } from "./sessionImport";

vi.mock("@/lib/sessionsApi", () => ({ importLocalSessions: vi.fn() }));

const importMock = vi.mocked(importLocalSessions);

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
    );
    stream.resolve(DONE);
    await first;
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
