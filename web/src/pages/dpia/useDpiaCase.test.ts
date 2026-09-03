import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as DpiaApiModule from "@/lib/dpia/dpiaApi";
import { createStudentSuccessAlertSeed } from "@/lib/dpia/seed";

const { loadMock, saveMock } = vi.hoisted(() => ({
  loadMock: vi.fn(),
  saveMock: vi.fn(),
}));

vi.mock("@/lib/dpia/dpiaApi", async (importOriginal) => ({
  ...(await importOriginal<typeof DpiaApiModule>()),
  loadDurableDpiaCase: loadMock,
  saveDurableDpiaCase: saveMock,
}));

import { DpiaCaseConflictError } from "@/lib/dpia/dpiaApi";
import { useDpiaCase } from "./useDpiaCase";

function durableCase(owner: string, revision: number) {
  const caseData = { ...createStudentSuccessAlertSeed(), owner };
  return {
    caseData,
    revision,
    createdBy: "officer@example.com",
    updatedBy: "officer@example.com",
    createdAt: 1,
    updatedAt: revision,
    source: "persisted" as const,
    recoveredInvalidState: false,
  };
}

beforeEach(() => {
  loadMock.mockReset();
  saveMock.mockReset();
});

describe("useDpiaCase durable writes", () => {
  it("waits for the durable revision before deriving the first mutation", async () => {
    let resolveLoad: (value: ReturnType<typeof durableCase>) => void = () => undefined;
    loadMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveLoad = resolve;
        }),
    );
    saveMock.mockImplementationOnce(async (snapshot) => ({
      ...durableCase("Initial owner", 2),
      caseData: snapshot,
    }));
    const { result } = renderHook(() => useDpiaCase("student-success-alert"));

    let mutation: Promise<unknown> = Promise.resolve();
    act(() => {
      mutation = result.current.bindSession("session-after-load");
    });
    expect(saveMock).not.toHaveBeenCalled();

    await act(async () => {
      resolveLoad(durableCase("Initial owner", 1));
      await mutation;
    });

    expect(saveMock).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "session-after-load" }),
      1,
    );
  });

  it("derives each queued mutation from the prior acknowledged revision", async () => {
    loadMock.mockResolvedValue(durableCase("Initial owner", 1));
    saveMock
      .mockImplementationOnce(async (snapshot) => ({
        ...durableCase("Initial owner", 2),
        caseData: snapshot,
      }))
      .mockImplementationOnce(async (snapshot) => ({
        ...durableCase("Initial owner", 3),
        caseData: snapshot,
      }));
    const { result } = renderHook(() => useDpiaCase("student-success-alert"));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    await act(async () => {
      await Promise.all([
        result.current.bindSession("session-1"),
        result.current.recordLiveRun({
          status: "failed",
          message: "Recorded after binding",
          updatedAt: "2026-08-29T12:00:02Z",
        }),
      ]);
    });

    expect(saveMock).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({ sessionId: "session-1" }),
      1,
    );
    expect(saveMock).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({
        sessionId: "session-1",
        liveRun: expect.objectContaining({ message: "Recorded after binding" }),
      }),
      2,
    );
    expect(result.current.caseData).toMatchObject({
      sessionId: "session-1",
      liveRun: { message: "Recorded after binding" },
    });
  });

  it("discards dependent queued writes and reloads after a revision conflict", async () => {
    loadMock
      .mockResolvedValueOnce(durableCase("Initial owner", 1))
      .mockResolvedValue(durableCase("Concurrent owner", 2));
    saveMock.mockRejectedValueOnce(new DpiaCaseConflictError(2));
    const { result } = renderHook(() => useDpiaCase("student-success-alert"));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    let outcomes: PromiseSettledResult<unknown>[] = [];
    await act(async () => {
      const actions = [
        result.current.recordLiveRun({
          status: "failed",
          message: "First optimistic update",
          updatedAt: "2026-08-29T12:00:00Z",
        }),
        result.current.recordLiveRun({
          status: "failed",
          message: "Dependent optimistic update",
          updatedAt: "2026-08-29T12:00:01Z",
        }),
      ];
      outcomes = await Promise.allSettled(actions);
    });

    await waitFor(() => expect(result.current.isSaving).toBe(false));
    expect(outcomes.every(({ status }) => status === "rejected")).toBe(true);
    expect(saveMock).toHaveBeenCalledTimes(1);
    expect(result.current.caseData.owner).toBe("Concurrent owner");
    expect(result.current.caseData.liveRun).toBeUndefined();
    expect(result.current.persistenceError).toContain("revision 2");
  });

  it("does not publish an old case save after the active case changes", async () => {
    let resolveSave: (value: ReturnType<typeof durableCase>) => void = () => undefined;
    loadMock
      .mockResolvedValueOnce(durableCase("First case", 1))
      .mockResolvedValueOnce(durableCase("Second case", 4));
    saveMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    const { result, rerender } = renderHook(({ caseId }) => useDpiaCase(caseId), {
      initialProps: { caseId: "student-success-alert" },
    });
    await waitFor(() => expect(result.current.caseData.owner).toBe("First case"));

    let oldMutation: Promise<unknown> = Promise.resolve();
    act(() => {
      oldMutation = result.current.bindSession("old-session");
    });
    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(1));
    rerender({ caseId: "second-case" });
    await waitFor(() => expect(result.current.caseData.owner).toBe("Second case"));

    await act(async () => {
      resolveSave(durableCase("Stale first case", 2));
      await oldMutation;
    });

    expect(result.current.caseData.owner).toBe("Second case");
    expect(result.current.isSaving).toBe(false);
  });
});
