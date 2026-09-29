// Saving through a reachable host can reconnect the runner. Only a workspace
// whose runner and host are both unavailable should buffer edits.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useWriteFileContent", () => ({ useWriteFileContent: vi.fn() }));
vi.mock("@/hooks/useFileContent", () => ({ fetchFileContent: vi.fn() }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(),
  useSessionHostOnline: vi.fn(),
}));
// The hook only reads conversationId/sessionStatus to gate the pre-write
// conflict check, which these tests never reach.
vi.mock("@/store/chatStore", () => ({
  useChatStore: (selector: (s: unknown) => unknown) =>
    selector({ conversationId: null, sessionStatus: "idle" }),
}));

import { useEditorAutoSave } from "./useEditorAutoSave";
import * as writeHook from "@/hooks/useWriteFileContent";
import * as runnerHook from "@/hooks/RunnerHealthProvider";

const CONV = "conv_save_gate";
const PATH = "notes.md";
const EDITED = "base edited";

let mutateAsync: ReturnType<typeof vi.fn>;

function renderAutoSave() {
  const baselineRef = { current: "base" };
  return renderHook(() =>
    useEditorAutoSave({
      conversationId: CONV,
      path: PATH,
      canEdit: true,
      isDirty: true,
      setDirty: () => {},
      hasExternalUpdate: false,
      markSaved: () => {},
      reconcileServerContent: () => false,
      dismissExternalUpdate: () => {},
      baselineRef,
      getContent: () => EDITED,
      isEditorDirty: () => true,
    }),
  );
}

beforeEach(() => {
  mutateAsync = vi.fn().mockResolvedValue(undefined);
  vi.mocked(writeHook.useWriteFileContent).mockReturnValue({
    isPending: false,
    isError: false,
    isSuccess: false,
    reset: vi.fn(),
    mutateAsync,
  } as unknown as ReturnType<typeof writeHook.useWriteFileContent>);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("useEditorAutoSave save gate", () => {
  it("attempts saving while the runner is offline but its host is online", async () => {
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(false);
    vi.mocked(runnerHook.useSessionHostOnline).mockReturnValue(true);
    const { result } = renderAutoSave();

    expect(result.current.saveDisabled).toBe(false);
    await act(async () => {
      result.current.autoSave.flush();
    });
    expect(mutateAsync).toHaveBeenCalledWith({ path: PATH, content: EDITED });
  });

  it("suppresses saving while the workspace is unreachable (runner and host down)", async () => {
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(false);
    vi.mocked(runnerHook.useSessionHostOnline).mockReturnValue(false);
    const { result } = renderAutoSave();

    expect(result.current.saveDisabled).toBe(true);
    await act(async () => {
      result.current.autoSave.flush();
    });
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("suppresses saving for a non-host-bound session whose runner is down", async () => {
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(false);
    vi.mocked(runnerHook.useSessionHostOnline).mockReturnValue(null);
    const { result } = renderAutoSave();

    expect(result.current.saveDisabled).toBe(true);
    await act(async () => {
      result.current.autoSave.flush();
    });
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("flushes a buffered edit once the workspace becomes reachable", async () => {
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(false);
    vi.mocked(runnerHook.useSessionHostOnline).mockReturnValue(null);
    const { result, rerender } = renderAutoSave();
    await act(async () => {
      result.current.autoSave.flush();
    });
    expect(mutateAsync).not.toHaveBeenCalled();

    vi.mocked(runnerHook.useSessionHostOnline).mockReturnValue(true);
    await act(async () => {
      rerender();
    });
    expect(mutateAsync).toHaveBeenCalledWith({ path: PATH, content: EDITED });
  });
});
