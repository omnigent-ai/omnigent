import { act, renderHook, waitFor } from "@testing-library/react";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as IdentityModule from "@/lib/identity";
import { getCurrentUserId } from "@/lib/identity";
import { readModelCatalogCache, sandboxSessionCatalogKey } from "@/lib/modelCatalogCache";
import type { NativeModelOption } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";
import {
  launchSelectionNotice,
  launchSelectionResets,
  markSandboxLaunch,
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
  // Launch checks are kept per session id, so each test uses its own session.
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
    { launched = true, enabled = true, modelLabel = null as string | null } = {},
  ) {
    if (launched) markSandboxLaunch(sessionId, modelLabel);
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

  it("resets the model and an effort outside the harness ladder together", async () => {
    const { rerender } = renderCheck("claude-native");
    rerender({ models: claudeModels });
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith(
        "gpt-5 isn't available in this sandbox. Using the default model and effort.",
      ),
    );
    expect(setModel).toHaveBeenCalledWith(null);
    expect(setEffort).toHaveBeenCalledWith(null);
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

  it("retires the check when the user leaves before the catalog arrives", async () => {
    markSandboxLaunch(sessionId, null);
    const hook = renderHook(
      ({ id, models }: { id: string; models: NativeModelOption[] }) =>
        useSandboxLaunchSelectionCheck(id, "codex-native", models, true),
      { initialProps: { id: sessionId, models: [] as NativeModelOption[] } },
    );
    act(() => useChatStore.setState({ conversationId: "conv_elsewhere" }));
    hook.rerender({ id: "conv_elsewhere", models: [] });
    // Reopening later and receiving the catalog must not reset anything.
    act(() => useChatStore.setState({ conversationId: sessionId }));
    hook.rerender({ id: sessionId, models: codexModels });
    await act(async () => {});
    expect(setModel).not.toHaveBeenCalled();
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

  it("saves the reported catalog per agent and refreshes a mounted reader", () => {
    const reader = renderHook(() =>
      useSavedSandboxSessionCatalog("arclet", "codex-native", "ag_default"),
    );
    expect(reader.result.current).toBeUndefined();
    const remember = renderHook(
      ({ models, enabled }: { models: NativeModelOption[]; enabled: boolean }) =>
        useRememberSandboxSessionCatalog("arclet", "codex-native", "ag_default", models, enabled),
      { initialProps: { models: codexModels, enabled: false } },
    );
    expect(reader.result.current).toBeUndefined();
    remember.rerender({ models: codexModels, enabled: true });
    expect(reader.result.current).toEqual(codexModels);
    remember.rerender({ models: [codexModels[0]], enabled: true });
    expect(reader.result.current).toEqual([codexModels[0]]);
    // An agent with its own provider keeps its catalog to itself.
    expect(
      readModelCatalogCache(sandboxSessionCatalogKey("arclet", "codex-native", "ag_private")),
    ).toBeNull();
  });
});
