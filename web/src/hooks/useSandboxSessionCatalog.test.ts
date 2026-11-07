import { act, renderHook, waitFor } from "@testing-library/react";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as IdentityModule from "@/lib/identity";
import { getCurrentUserId } from "@/lib/identity";
import {
  readModelCatalogCache,
  SANDBOX_SESSION_CATALOG_MAX_AGE_MS,
  sandboxSessionCatalogKey,
} from "@/lib/modelCatalogCache";
import { markLiveSessionCatalog, markSandboxLaunch } from "@/lib/sandboxSessionProvenance";
import type { NativeModelOption } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";
import {
  launchSelectionNotice,
  launchSelectionResets,
  useSandboxLaunchSelectionCheck,
} from "./useSandboxLaunchSelectionCheck";
import {
  useRememberSandboxSessionCatalog,
  useSavedSandboxSessionCatalog,
} from "./useSandboxSessionCatalog";

vi.mock("sonner", () => ({ toast: { info: vi.fn(), error: vi.fn() } }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: vi.fn(),
}));

const codexModels: NativeModelOption[] = [
  {
    id: "gpt-6-luna",
    displayName: "GPT 6 Luna",
    isDefault: true,
    supportedReasoningEfforts: [{ reasoningEffort: "low" }, { reasoningEffort: "high" }],
  },
  { id: "kimi-k3", displayName: "Kimi K3", supportedReasoningEfforts: [] },
];

describe("launchSelectionResets", () => {
  it.each([
    ["an offered model and effort", "codex-native", "gpt-6-luna", "high", false, false],
    ["no explicit choice", "codex-native", null, null, false, false],
    ["a model the session lacks", "codex-native", "gpt-5", null, true, false],
    ["an effort the model lacks", "codex-native", "gpt-6-luna", "minimal", false, true],
    ["a model with no effort ladder", "codex-native", "kimi-k3", "high", false, false],
    ["an effort after the model resets", "codex-native", "gpt-5", "minimal", true, false],
    ["an effort with no model override", "codex-native", null, "minimal", false, false],
    ["a Fusion combo", "devin-native", "fusion-a1", "minimal", false, false],
    ["Claude's in-session ladder", "claude-native", null, "xhigh", false, false],
    ["an effort outside Claude's ladder", "claude-native", null, "minimal", false, true],
  ])("handles %s", (_label, harness, model, effort, resetModel, resetEffort) => {
    const models =
      harness === "claude-native" ? [{ id: "opus", displayName: "Opus" }] : codexModels;
    expect(launchSelectionResets(harness, models, model, effort)).toEqual({
      model: resetModel,
      effort: resetEffort,
    });
  });

  it("matches a provider-facing model id and leaves an empty catalog alone", () => {
    const models = [{ id: "opus", model: "system.ai.claude-opus-5" }];
    expect(launchSelectionResets("claude-native", models, "system.ai.claude-opus-5", null)).toEqual(
      { model: false, effort: false },
    );
    expect(launchSelectionResets("codex-native", [], "anything", "none")).toEqual({
      model: false,
      effort: false,
    });
  });
});

describe("launchSelectionNotice", () => {
  it("names what was replaced", () => {
    expect(launchSelectionNotice({ model: true, effort: true }, "gpt-5", "minimal")).toBe(
      "gpt-5 isn't available in this sandbox. Using the default model and effort.",
    );
    expect(launchSelectionNotice({ model: false, effort: true }, null, "xhigh")).toBe(
      "xHigh effort isn't available for this model in this sandbox. Using the default effort.",
    );
    expect(launchSelectionNotice({ model: false, effort: false }, "gpt-5", null)).toBeNull();
  });
});

describe("sandbox session catalog hooks", () => {
  const setModel = vi.fn(async () => {});
  const setEffort = vi.fn(async () => {});
  const original = useChatStore.getState();
  // The store keeps settings per conversation id, so each test uses its own session.
  let testIndex = 0;
  let sessionId = "conv_0";

  beforeEach(() => {
    localStorage.clear();
    vi.mocked(getCurrentUserId).mockReturnValue("alice");
    testIndex += 1;
    sessionId = `conv_${testIndex}`;
    useChatStore.setState({
      conversationId: sessionId,
      sessionModelOverride: "gpt-5",
      sessionReasoningEffort: "minimal",
      setModel,
      setEffort,
    });
  });
  afterEach(() => {
    act(() => useChatStore.setState(original, true));
    vi.clearAllMocks();
  });

  const claudeModels: NativeModelOption[] = [{ id: "opus", displayName: "Opus" }];

  function renderCheck(
    harness = "codex-native",
    {
      launched = true,
      live = true,
      enabled = true,
      launchModel = "gpt-5" as string | null,
      modelLabel = null as string | null,
    } = {},
  ) {
    if (launched) markSandboxLaunch(sessionId, { model: launchModel, modelLabel });
    if (live) markLiveSessionCatalog(sessionId, []);
    return renderHook(
      ({ models }: { models: NativeModelOption[] }) =>
        useSandboxLaunchSelectionCheck(sessionId, harness, models, enabled),
      { initialProps: { models: [] as NativeModelOption[] } },
    );
  }

  it("resets unavailable launch choices once the launched session reports its catalog", async () => {
    const { rerender } = renderCheck("codex-native", { modelLabel: "GPT-5" });
    expect(setModel).not.toHaveBeenCalled();
    rerender({ models: codexModels });
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith(
        "GPT-5 isn't available in this sandbox. Using the default model.",
      ),
    );
    expect(setModel).toHaveBeenCalledWith(null);
    expect(setEffort).not.toHaveBeenCalled();
    // Later catalog refreshes do not recheck the user's in-session choices.
    rerender({ models: [...codexModels] });
    expect(setModel).toHaveBeenCalledTimes(1);
  });

  it("names the current model when the user switched after launch", async () => {
    const { rerender } = renderCheck("codex-native", {
      launchModel: "gpt-4o",
      modelLabel: "GPT-4o",
    });
    rerender({ models: codexModels });
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith(
        "gpt-5 isn't available in this sandbox. Using the default model.",
      ),
    );
  });

  it("keeps an effort picked while the model reset is pending", async () => {
    let finishModelReset = () => {};
    setModel.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finishModelReset = resolve;
        }),
    );
    const { rerender } = renderCheck("claude-native");
    rerender({ models: claudeModels });
    await waitFor(() => expect(setModel).toHaveBeenCalledWith(null));
    act(() => useChatStore.setState({ sessionReasoningEffort: "high" }));
    await act(async () => finishModelReset());
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith(
        "gpt-5 isn't available in this sandbox. Using the default model.",
      ),
    );
    expect(setEffort).not.toHaveBeenCalled();
  });

  it("finishes an effort reset when the user returns to the session", async () => {
    setModel.mockImplementationOnce(async () => {
      useChatStore.setState({ sessionModelOverride: null });
      useChatStore.setState({ conversationId: "conv_other" });
    });
    const { rerender } = renderCheck("claude-native");
    rerender({ models: claudeModels });
    await waitFor(() => expect(setModel).toHaveBeenCalledWith(null));
    expect(setEffort).not.toHaveBeenCalled();
    expect(toast.info).not.toHaveBeenCalled();
    act(() => useChatStore.setState({ conversationId: sessionId }));
    rerender({ models: claudeModels });
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith(
        "Minimal effort isn't available for this model in this sandbox. Using the default effort.",
      ),
    );
    expect(toast.info).toHaveBeenCalledWith(
      "gpt-5 isn't available in this sandbox. Using the default model.",
    );
    expect(setEffort).toHaveBeenCalledWith(null);
    expect(setModel).toHaveBeenCalledTimes(1);
  });

  it("shows a reset's notice when the user returns to the session", async () => {
    useChatStore.setState({ sessionReasoningEffort: null });
    setModel.mockImplementationOnce(async () => {
      useChatStore.setState({ conversationId: "conv_other" });
    });
    const { rerender } = renderCheck();
    rerender({ models: codexModels });
    await waitFor(() => expect(setModel).toHaveBeenCalledWith(null));
    await act(async () => {});
    expect(toast.info).not.toHaveBeenCalled();
    act(() => useChatStore.setState({ conversationId: sessionId }));
    rerender({ models: codexModels });
    expect(toast.info).toHaveBeenCalledExactlyOnceWith(
      "gpt-5 isn't available in this sandbox. Using the default model.",
    );
  });

  it("names the effort when only its reset fails", async () => {
    setEffort.mockRejectedValueOnce(new Error("refused"));
    const { rerender } = renderCheck("claude-native");
    rerender({ models: claudeModels });
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Couldn't switch to the sandbox's default effort."),
    );
    expect(toast.info).toHaveBeenCalledExactlyOnceWith(
      "gpt-5 isn't available in this sandbox. Using the default model.",
    );
  });

  it("names the effort when the model needed no change and the effort reset fails", async () => {
    useChatStore.setState({ sessionModelOverride: "opus" });
    setEffort.mockRejectedValueOnce(new Error("refused"));
    const { rerender } = renderCheck("claude-native");
    rerender({ models: claudeModels });
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Couldn't switch to the sandbox's default effort."),
    );
    expect(setModel).not.toHaveBeenCalled();
  });

  it("leaves later choices alone after a launch the sandbox supports", async () => {
    useChatStore.setState({ sessionModelOverride: "opus", sessionReasoningEffort: null });
    const { rerender } = renderCheck("claude-native");
    rerender({ models: claudeModels });
    await act(async () => {});
    act(() => useChatStore.setState({ sessionModelOverride: "gpt-5" }));
    rerender({ models: [...claudeModels] });
    await act(async () => {});
    expect(setModel).not.toHaveBeenCalled();
    expect(toast.info).not.toHaveBeenCalled();
  });

  it("reports a reset the server refused", async () => {
    setModel.mockRejectedValueOnce(new Error("refused"));
    const { rerender } = renderCheck();
    rerender({ models: codexModels });
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Couldn't switch to the sandbox's default model settings.",
      ),
    );
    expect(toast.info).not.toHaveBeenCalled();
  });

  it.each([
    ["a session this tab did not launch, such as one waking", { launched: false }],
    ["a session with configured inference", { enabled: false }],
    ["a catalog read only from a session snapshot", { live: false }],
  ])("leaves %s alone", (_label, options) => {
    const { rerender } = renderCheck("codex-native", options);
    rerender({ models: codexModels });
    expect(setModel).not.toHaveBeenCalled();
    expect(toast.info).not.toHaveBeenCalled();
  });

  function renderRemember(rememberedSessionId: string, enabled = true) {
    return renderHook(
      ({ active }: { active: boolean }) =>
        useRememberSandboxSessionCatalog("arclet", "codex-native", rememberedSessionId, active),
      { initialProps: { active: enabled } },
    );
  }

  it("saves each catalog the runner reports once, under the provider and harness", () => {
    const key = sandboxSessionCatalogKey("arclet", "codex-native");
    const { rerender } = renderRemember(sessionId, false);
    act(() => markLiveSessionCatalog(sessionId, []));
    act(() => markLiveSessionCatalog(sessionId, codexModels));
    expect(readModelCatalogCache(key)).toBeNull();
    rerender({ active: true });
    expect(readModelCatalogCache(key)?.models).toEqual(codexModels);
    // A repeated report with the same rows leaves the entry alone.
    const stored = localStorage.getItem(key!);
    const clock = vi.spyOn(Date, "now").mockReturnValue(Date.now() + 60_000);
    act(() => markLiveSessionCatalog(sessionId, [...codexModels]));
    expect(localStorage.getItem(key!)).toBe(stored);
    act(() => markLiveSessionCatalog(sessionId, [codexModels[0]]));
    expect(readModelCatalogCache(key)?.models).toEqual([codexModels[0]]);
    clock.mockRestore();
  });

  it("never republishes a reopened session's snapshot over a newer report", () => {
    const key = sandboxSessionCatalogKey("arclet", "codex-native");
    act(() => markLiveSessionCatalog(sessionId, codexModels));
    renderRemember(sessionId).unmount();
    act(() => markLiveSessionCatalog("conv_newer", [codexModels[0]]));
    renderRemember("conv_newer").unmount();
    const stored = localStorage.getItem(key!);
    const clock = vi.spyOn(Date, "now").mockReturnValue(Date.now() + 2 * 24 * 60 * 60 * 1000);
    // Reopening the first session only rehydrates its snapshot; nothing new was reported.
    renderRemember(sessionId);
    expect(localStorage.getItem(key!)).toBe(stored);
    clock.mockRestore();
  });

  it("refreshes a mounted reader when a session catalog is saved or expires", () => {
    vi.useFakeTimers();
    try {
      const reader = renderHook(() => useSavedSandboxSessionCatalog("arclet", "codex-native"));
      expect(reader.result.current).toBeUndefined();
      act(() => markLiveSessionCatalog(sessionId, codexModels));
      renderRemember(sessionId);
      expect(reader.result.current).toEqual(codexModels);
      act(() => vi.advanceTimersByTime(SANDBOX_SESSION_CATALOG_MAX_AGE_MS + 1_000));
      expect(reader.result.current).toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  });
});
