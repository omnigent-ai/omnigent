import { act, renderHook, waitFor } from "@testing-library/react";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as IdentityModule from "@/lib/identity";
import { getCurrentUserId } from "@/lib/identity";
import { readModelCatalogCache, sandboxSessionCatalogKey } from "@/lib/modelCatalogCache";
import { markSandboxLaunch } from "@/lib/sandboxSessionProvenance";
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

  function renderCheck(harness = "codex-native", { launched = true, enabled = true } = {}) {
    if (launched) markSandboxLaunch(sessionId);
    return renderHook(
      ({ models }: { models: NativeModelOption[] }) =>
        useSandboxLaunchSelectionCheck(sessionId, harness, models, enabled),
      { initialProps: { models: [] as NativeModelOption[] } },
    );
  }

  it("resets unavailable launch choices once the launched session reports its catalog", async () => {
    const { rerender } = renderCheck();
    expect(setModel).not.toHaveBeenCalled();
    rerender({ models: codexModels });
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith(
        "gpt-5 isn't available in this sandbox. Using the default model.",
      ),
    );
    expect(setModel).toHaveBeenCalledWith(null);
    expect(setEffort).not.toHaveBeenCalled();
    // Later catalog refreshes do not recheck the user's in-session choices.
    rerender({ models: [...codexModels] });
    expect(setModel).toHaveBeenCalledTimes(1);
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
    expect(setEffort).toHaveBeenCalledWith(null);
    expect(setModel).toHaveBeenCalledTimes(1);
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
  ])("leaves %s alone", (_label, options) => {
    const { rerender } = renderCheck("codex-native", options);
    rerender({ models: codexModels });
    expect(setModel).not.toHaveBeenCalled();
    expect(toast.info).not.toHaveBeenCalled();
  });

  it("saves only a non-empty session catalog under the provider and harness", () => {
    const key = sandboxSessionCatalogKey("arclet", "codex-native");
    const { rerender } = renderHook(
      ({ models, enabled }: { models: NativeModelOption[]; enabled: boolean }) =>
        useRememberSandboxSessionCatalog("arclet", "codex-native", models, enabled),
      { initialProps: { models: [] as NativeModelOption[], enabled: true } },
    );
    expect(readModelCatalogCache(key)).toBeNull();
    rerender({ models: codexModels, enabled: false });
    expect(readModelCatalogCache(key)).toBeNull();
    rerender({ models: codexModels, enabled: true });
    expect(readModelCatalogCache(key)?.models).toEqual(codexModels);
  });

  it("refreshes a mounted reader when a session catalog is saved", () => {
    const reader = renderHook(() => useSavedSandboxSessionCatalog("arclet", "codex-native"));
    expect(reader.result.current).toBeUndefined();
    renderHook(() => useRememberSandboxSessionCatalog("arclet", "codex-native", codexModels, true));
    expect(reader.result.current).toEqual(codexModels);
  });
});
