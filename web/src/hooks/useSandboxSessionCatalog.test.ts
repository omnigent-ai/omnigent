import { act, renderHook, waitFor } from "@testing-library/react";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as IdentityModule from "@/lib/identity";
import { getCurrentUserId } from "@/lib/identity";
import { readModelCatalogCache, sandboxSessionCatalogKey } from "@/lib/modelCatalogCache";
import type { NativeModelOption, SandboxStatus } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";
import {
  launchSelectionNotice,
  launchSelectionResets,
  useSandboxLaunchSelectionCheck,
} from "./useSandboxLaunchSelectionCheck";
import { useRememberSandboxSessionCatalog } from "./useSandboxSessionCatalog";

vi.mock("sonner", () => ({ toast: { info: vi.fn() } }));
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
    ["an effort the model lacks", "codex-native", "kimi-k3", "high", false, true],
    ["an effort the default model lacks", "codex-native", "gpt-5", "minimal", true, true],
    ["an effort the default model offers", "codex-native", "gpt-5", "low", true, false],
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

  beforeEach(() => {
    localStorage.clear();
    vi.mocked(getCurrentUserId).mockReturnValue("alice");
    useChatStore.setState({
      conversationId: "conv_1",
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

  function renderCheck(sessionId: string | null = "conv_1") {
    return renderHook(
      ({ status, models }: { status: SandboxStatus | null; models: NativeModelOption[] }) =>
        useSandboxLaunchSelectionCheck(sessionId, status, "codex-native", models),
      { initialProps: { status: { stage: "provisioning", provider: "arclet" }, models: [] } },
    );
  }

  it("resets unavailable launch choices once the launched session reports its catalog", async () => {
    const { rerender } = renderCheck();
    expect(setModel).not.toHaveBeenCalled();
    rerender({ status: null, models: codexModels });
    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith(
        "gpt-5 isn't available in this sandbox. Using the default model and effort.",
      ),
    );
    expect(setModel).toHaveBeenCalledWith(null);
    expect(setEffort).toHaveBeenCalledWith(null);
    // Later catalog refreshes do not recheck the user's in-session choices.
    rerender({ status: null, models: [...codexModels] });
    expect(setModel).toHaveBeenCalledTimes(1);
  });

  it("leaves a session it never saw launching alone", () => {
    const { rerender } = renderHook(
      ({ models }: { models: NativeModelOption[] }) =>
        useSandboxLaunchSelectionCheck("conv_1", null, "codex-native", models),
      { initialProps: { models: [] as NativeModelOption[] } },
    );
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
});
