import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type * as AgentLabels from "@/lib/agentLabels";
import type { NativeModelOption, Session } from "@/lib/types";
import { useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useSessionPickerInputs } from "./sessionPickerInputs";

// The catalog lookups are network queries. Each test sets the probe's answer, and the
// harness the hook asked it for is recorded here.
const hostProbe = vi.hoisted(() => ({
  options: undefined as NativeModelOption[] | undefined,
  harness: "",
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: () => ({ data: undefined }),
  useHostModelOptions: (_hostId: string | null, harness: string) => {
    hostProbe.harness = harness;
    return { data: hostProbe.options };
  },
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
  hostProbe.options = undefined;
  hostProbe.harness = "";
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

it("uses the launch preview only while the active conversation has no catalog", () => {
  const PREVIEW = [codexModel("gpt-preview", "medium")];
  useChatStore.setState({ conversationId: "active", codexModelOptions: [] });
  // One mounted consumer, so the catalog's arrival is seen as a live switch.
  const { result } = renderHook(() =>
    useSessionPickerInputs(undefined, null, { launchPreviewOptions: PREVIEW }),
  );
  expect(result.current.codexModelOptions).toBe(PREVIEW);
  expect(result.current.modelsFromLaunchPreview).toBe(true);

  act(() => useChatStore.setState({ codexModelOptions: ACTIVE_OPTIONS }));
  expect(result.current.codexModelOptions).toBe(ACTIVE_OPTIONS);
  expect(result.current.modelsFromLaunchPreview).toBe(false);
});

it("prefers the host's probed catalog over the launch preview, unless the session is configured", () => {
  const PROBED = [codexModel("gpt-probed", "xhigh")];
  const PREVIEW = [codexModel("gpt-preview", "medium")];
  hostProbe.options = PROBED;
  useChatStore.setState({ conversationId: "active", codexModelOptions: [] });
  const session = {
    id: "active",
    hostId: "host-1",
    harness: "codex-native",
    labels: {},
    agentName: null,
    parentSessionId: null,
  } as unknown as Session;

  const { result } = renderHook(() =>
    useSessionPickerInputs(undefined, session, { launchPreviewOptions: PREVIEW }),
  );
  expect(hostProbe.harness).toBe("codex-native");
  expect(result.current.codexModelOptions).toBe(PROBED);
  expect(result.current.modelsFromLaunchPreview).toBe(false);

  const configured = renderHook(() =>
    useSessionPickerInputs(
      undefined,
      { ...session, inferenceConfigured: true },
      {
        launchPreviewOptions: PREVIEW,
      },
    ),
  );
  expect(configured.result.current.codexModelOptions).toBe(
    useChatStore.getState().codexModelOptions,
  );
});
