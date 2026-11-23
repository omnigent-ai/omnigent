import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import {
  chooseSettingValue,
  expectSetting,
  registeredPaletteCommands,
} from "@/test/paletteCommands";
import type * as SectionSettingsModule from "./composerSectionSettings";
import { SessionHarnessPicker } from "./SessionHarnessPicker";

// Records each render of the ⌘K settings, so a picker without a source can prove it never mounts them.
const settingsRenders = vi.hoisted(() => ({ count: 0 }));
vi.mock("./composerSectionSettings", async (importOriginal) => {
  const original = await importOriginal<typeof SectionSettingsModule>();
  return {
    ...original,
    ComposerPaletteSettings: (props: ComponentProps<typeof original.ComposerPaletteSettings>) => {
      settingsRenders.count += 1;
      return original.ComposerPaletteSettings(props);
    },
  };
});

const OPTIONS = [
  { id: "opus", displayName: "Opus" },
  { id: "sonnet", displayName: "Sonnet" },
];
const initialState = useChatStore.getState();

afterEach(() => {
  cleanup();
  conversationRegistry.clear();
  vi.restoreAllMocks();
  useChatStore.setState(initialState, true);
  usePaletteCommandsStore.setState({ groups: {} });
  settingsRenders.count = 0;
});

/** The open session's picker, as ChatPage's composer renders it, with `quick_pick_palette` on. */
function renderOpenSessionPicker(paletteSettingsSource?: string) {
  return render(openSessionPicker(paletteSettingsSource));
}

function openSessionPicker(paletteSettingsSource?: string) {
  return (
    <CapabilitiesProvider
      info={{ ...FALLBACK_SERVER_INFO, features: { quick_pick_palette: true } }}
    >
      <TooltipProvider>
        <SessionHarnessPicker
          busy={false}
          busyRef={{ current: false }}
          setBusy={() => {}}
          agentName={null}
          harnessLabel="Claude Code"
          showModels
          showEffort
          effortLevels={["low", "high"]}
          modelPickerKind="claude"
          supportsModelReset={false}
          codexModelOptions={OPTIONS}
          modelLabelOptions={OPTIONS}
          modelLabelHostId={null}
          costRoutingEligible
          modelPickWakesSession={false}
          disabled={false}
          paletteSettingsSource={paletteSettingsSource}
        />
      </TooltipProvider>
    </CapabilitiesProvider>
  );
}

/** Markup without React's generated ids, which count every `useId` call in the tree. */
const markup = (element: HTMLElement) => element.innerHTML.replace(/:r[0-9a-z]+:/g, ":id:");

it("without a ⌘K source, mounts none of the settings and renders the same picker", () => {
  useChatStore.setState({ conversationId: "active", llmModel: "opus" });
  const { container, unmount } = renderOpenSessionPicker();
  const withoutSource = markup(container);
  expect(settingsRenders.count).toBe(0);
  expect(registeredPaletteCommands()).toEqual([]);
  unmount();

  // The settings render nothing themselves, so a source changes no markup either.
  const withSource = renderOpenSessionPicker("composer:active");
  expect(settingsRenders.count).toBeGreaterThan(0);
  expect(markup(withSource.container)).toBe(withoutSource);
});

it("keeps the same picker when a new chat's draft becomes a session and gains a ⌘K source", () => {
  useChatStore.setState({ conversationId: "active", llmModel: "opus" });
  const { container, rerender } = renderOpenSessionPicker();
  const picker = container.firstElementChild;
  expect(picker).not.toBeNull();
  rerender(openSessionPicker("composer:active"));
  expect(settingsRenders.count).toBeGreaterThan(0);
  // A remount would replace the picker's nodes and drop its open menu and focus.
  expect(container.firstElementChild).toBe(picker);
});

it("with a ⌘K source, offers Model and Reasoning Effort that change the session as the menu does", async () => {
  useChatStore.setState({ conversationId: "active", llmModel: "opus" });
  const store = useChatStore.getState();
  const setModel = vi.spyOn(store, "setModel").mockResolvedValue();
  const setEffort = vi.spyOn(store, "setEffort").mockResolvedValue();
  const setCostControlMode = vi.spyOn(store, "setCostControlMode").mockResolvedValue();
  renderOpenSessionPicker("composer:active");

  const model = expectSetting(registeredPaletteCommands(), "Model", {
    current: "Opus",
    choices: ["Smart Routing", "Opus", "Sonnet"],
  });
  await act(async () => chooseSettingValue(model, "Sonnet"));
  await waitFor(() =>
    expect(setModel).toHaveBeenCalledWith("sonnet", { expectConfirmation: true }),
  );
  await act(async () => chooseSettingValue(model, "Smart Routing"));
  await waitFor(() => expect(setCostControlMode).toHaveBeenCalledWith("on"));

  const effort = expectSetting(registeredPaletteCommands(), "Reasoning Effort", {
    choices: ["Low", "High"],
  });
  await act(async () => chooseSettingValue(effort, "High"));
  await waitFor(() => expect(setEffort).toHaveBeenCalledWith("high"));
});

it("shows and changes the given session, not the active conversation", async () => {
  useChatStore.setState({ conversationId: "active", llmModel: "opus" });
  conversationRegistry.acquire("cell").setState({
    sessionHarness: "claude-native",
    llmModel: "sonnet",
    costControlModeOverride: "on",
  });
  const store = useChatStore.getState();
  const setModel = vi.spyOn(store, "setModel").mockResolvedValue();
  const setEffort = vi.spyOn(store, "setEffort").mockResolvedValue();
  const setCostControlMode = vi.spyOn(store, "setCostControlMode").mockResolvedValue();
  render(
    <TooltipProvider>
      <SessionHarnessPicker
        sessionId="cell"
        busy={false}
        busyRef={{ current: false }}
        setBusy={() => {}}
        agentName={null}
        harnessLabel="Claude Code"
        showModels
        showEffort
        effortLevels={["low", "high"]}
        modelPickerKind="claude"
        supportsModelReset={false}
        codexModelOptions={OPTIONS}
        modelLabelOptions={OPTIONS}
        modelLabelHostId={null}
        costRoutingEligible
        modelPickWakesSession={false}
        disabled={false}
      />
    </TooltipProvider>,
  );
  const openMenu = () => {
    fireEvent.keyDown(screen.getByTestId("composer-config-gear"), { key: "ArrowDown" });
    fireEvent.click(screen.getByTestId("composer-agent-edit"));
  };

  expect(screen.getByTestId("composer-config-gear")).toHaveTextContent("Smart Routing");
  openMenu();
  fireEvent.click(screen.getByTestId("composer-agent-model-opus"));
  // Picking a model with routing on also turns this session's routing off.
  await waitFor(() => expect(setCostControlMode).toHaveBeenCalledWith("off", "cell"));
  expect(setModel).toHaveBeenCalledWith("opus", { expectConfirmation: true, sessionId: "cell" });

  act(() => conversationRegistry.peek("cell")!.setState({ costControlModeOverride: null }));
  expect(screen.getByTestId("composer-config-gear")).toHaveTextContent("Sonnet");
  openMenu();
  fireEvent.click(screen.getByTestId("composer-agent-effort-high"));
  await waitFor(() => expect(setEffort).toHaveBeenCalledWith("high", "cell"));
});
