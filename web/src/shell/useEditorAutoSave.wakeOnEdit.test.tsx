// Unit tests for useEditorAutoSave's wake-on-edit behavior: a dirty editor in
// a session whose runner reads offline must request a server-side reconnect
// (retrySession) instead of stranding the buffered edit until the user sends a
// chat message. One wake per offline episode, re-armed on reconnect, and a
// failed wake re-arms so a later transition can retry.

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useWriteFileContent", () => ({ useWriteFileContent: vi.fn() }));
vi.mock("@/hooks/useFileContent", () => ({ fetchFileContent: vi.fn() }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({ useSessionRunnerOnline: vi.fn() }));
vi.mock("@/lib/sessionsApi", () => ({ retrySession: vi.fn() }));
// The hook only reads conversationId/sessionStatus to gate the pre-write
// conflict check, which these tests never reach.
vi.mock("@/store/chatStore", () => ({
  useChatStore: (selector: (s: unknown) => unknown) =>
    selector({ conversationId: null, sessionStatus: "idle" }),
}));

import { useEditorAutoSave } from "./useEditorAutoSave";
import * as writeHook from "@/hooks/useWriteFileContent";
import * as runnerHook from "@/hooks/RunnerHealthProvider";
import * as sessionsApi from "@/lib/sessionsApi";

const CONV = "conv_wake_on_edit";

interface HarnessProps {
  canEdit: boolean;
  isDirty: boolean;
}

function renderAutoSave(initial: Partial<HarnessProps> = {}) {
  const baselineRef = { current: "base" };
  return renderHook(
    (props: HarnessProps) =>
      useEditorAutoSave({
        conversationId: CONV,
        path: "notes.md",
        canEdit: props.canEdit,
        isDirty: props.isDirty,
        setDirty: () => {},
        hasExternalUpdate: false,
        markSaved: () => {},
        reconcileServerContent: () => false,
        dismissExternalUpdate: () => {},
        baselineRef,
        getContent: () => "base edited",
        isEditorDirty: () => props.isDirty,
      }),
    { initialProps: { canEdit: true, isDirty: false, ...initial } },
  );
}

beforeEach(() => {
  vi.mocked(writeHook.useWriteFileContent).mockReturnValue({
    isPending: false,
    isError: false,
    isSuccess: false,
    reset: vi.fn(),
    mutateAsync: vi.fn().mockResolvedValue(undefined),
  } as unknown as ReturnType<typeof writeHook.useWriteFileContent>);
  vi.mocked(sessionsApi.retrySession).mockResolvedValue({
    queued: false,
    recovered: true,
    recovery: "runner_relaunched",
  } as Awaited<ReturnType<typeof sessionsApi.retrySession>>);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("useEditorAutoSave wake-on-edit", () => {
  it("requests a runner reconnect when the editor goes dirty while offline", async () => {
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(false);
    const { rerender } = renderAutoSave();
    expect(sessionsApi.retrySession).not.toHaveBeenCalled();

    await act(async () => {
      rerender({ canEdit: true, isDirty: true });
    });
    expect(sessionsApi.retrySession).toHaveBeenCalledExactlyOnceWith(CONV);
  });

  it("wakes only once per offline episode", async () => {
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(false);
    const { rerender } = renderAutoSave({ isDirty: true });
    // Further dirty renders while still offline must not stack wake requests.
    await act(async () => {
      rerender({ canEdit: true, isDirty: false });
      rerender({ canEdit: true, isDirty: true });
    });
    expect(sessionsApi.retrySession).toHaveBeenCalledTimes(1);
  });

  it("re-arms after a reconnect so the next offline episode wakes again", async () => {
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(false);
    const { rerender } = renderAutoSave({ isDirty: true });
    expect(sessionsApi.retrySession).toHaveBeenCalledTimes(1);

    // Runner comes back (wake landed) — the guard re-arms.
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(true);
    await act(async () => {
      rerender({ canEdit: true, isDirty: false });
    });

    // A second offline episode with a fresh edit wakes again.
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(false);
    await act(async () => {
      rerender({ canEdit: true, isDirty: true });
    });
    expect(sessionsApi.retrySession).toHaveBeenCalledTimes(2);
  });

  it("does not wake while the runner is online or unobserved", async () => {
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(true);
    const online = renderAutoSave({ isDirty: true });
    online.unmount();

    // `undefined` = liveness not yet polled; must not fire a speculative wake.
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(undefined);
    renderAutoSave({ isDirty: true });

    expect(sessionsApi.retrySession).not.toHaveBeenCalled();
  });

  it("does not wake for a clean or read-only editor", async () => {
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(false);
    const clean = renderAutoSave({ isDirty: false });
    clean.unmount();
    const readOnly = renderAutoSave({ canEdit: false, isDirty: true });
    readOnly.unmount();

    expect(sessionsApi.retrySession).not.toHaveBeenCalled();
  });

  it("re-arms after a failed wake so a later transition can retry", async () => {
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(false);
    vi.mocked(sessionsApi.retrySession).mockRejectedValueOnce(new Error("503"));
    const { rerender } = renderAutoSave({ isDirty: true });
    await act(async () => {}); // let the rejection settle and re-arm
    expect(sessionsApi.retrySession).toHaveBeenCalledTimes(1);

    // A later dirty transition retries the wake.
    await act(async () => {
      rerender({ canEdit: true, isDirty: false });
    });
    await act(async () => {
      rerender({ canEdit: true, isDirty: true });
    });
    expect(sessionsApi.retrySession).toHaveBeenCalledTimes(2);
  });
});
