import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type * as AgentLabels from "@/lib/agentLabels";
import type { NativeModelOption, Session } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useSessionPickerInputs } from "./sessionPickerInputs";

// The catalog lookups are network queries; this test reads only the session's own state.
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: undefined }),
  useHostModelOptions: () => ({ data: undefined }),
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabels>()),
  useHarnessEffortFamilies: () => ({}),
}));

const initialState = useChatStore.getState();

function codexModel(id: string, effort: string): NativeModelOption {
  return { id, supportedReasoningEfforts: [{ reasoningEffort: effort }] };
}
const ACTIVE_OPTIONS = [codexModel("gpt-active", "low")];
const CELL_OPTIONS = [codexModel("gpt-cell", "high")];

afterEach(() => {
  cleanup();
  conversationRegistry.clear();
  useChatStore.setState(initialState, true);
});

it("reads a background session's options and model, not the active conversation's", () => {
  useChatStore.setState({
    conversationId: "active",
    codexModelOptions: ACTIVE_OPTIONS,
    llmModel: "gpt-active",
  });
  conversationRegistry.acquire("cell").setState({
    codexModelOptions: CELL_OPTIONS,
    llmModel: "gpt-cell",
  });
  const session = {
    id: "cell",
    harness: "codex-native",
    labels: {},
    agentName: null,
    parentSessionId: null,
  } as unknown as Session;

  const { result } = renderHook(() => useSessionPickerInputs("cell", session));

  expect(result.current.modelPickerKind).toBe("codex");
  expect(result.current.modelLabelOptions).toBe(CELL_OPTIONS);
  expect(result.current.effortLevels).toEqual(["high"]);
});
