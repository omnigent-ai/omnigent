// A setting in ⌘K: one row showing its value, a page of every value, and the
// way back. The setting here is the test's own, so each case controls its value,
// owner, availability and how a choice settles.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useMemo, useState } from "react";
import { SquareTerminalIcon } from "lucide-react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { create } from "zustand";

import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  unavailableBecause,
  usePaletteCommands,
} from "@/store/paletteCommands";
import { type PaletteSetting, type SettingOutcome, settingCommand } from "@/store/paletteSettings";

import { CommandContextProvider } from "./commandContext";
import { paletteChildPageStore } from "./commandPaletteChildPage";
import { QuickPickCommandPalette } from "./QuickPickCommandPalette";
import {
  CHOICE_UNCONFIRMED_STATUS,
  SETTING_GONE_NOTICE,
  settingFailedStatus,
} from "./settingChoicesPage";

vi.mock("@/lib/routing", () => ({
  useNavigate: () => vi.fn(),
  useLocation: () => ({ pathname: "/", search: "", hash: "", state: null, key: "default" }),
}));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ session_search_scope: true }),
}));
vi.mock("@/hooks/useConversations", () => ({
  useConversations: () => ({ data: { pages: [] }, isFetching: false }),
  useSessionSearch: () => ({ isFetching: false }),
  useProjects: () => ({ data: [] }),
}));

/** The test setting's state, as its owner would hold it. */
const engine = create<{
  value: string;
  owner: string;
  offered: boolean;
  availability: CommandAvailability;
  vulkanAvailability: CommandAvailability;
  reselect: "skip" | "apply";
  /** Set to make choices wait on a promise the test settles. */
  pending: boolean;
}>(() => ({
  value: "automatic",
  owner: "app",
  offered: true,
  availability: AVAILABLE,
  vulkanAvailability: AVAILABLE,
  reselect: "skip",
  pending: false,
}));
const chosen = vi.fn<(value: string) => void>();
let settleChoice: ((outcome: SettingOutcome) => void) | null = null;
let failChoice: ((error: Error) => void) | null = null;

function EngineSetting() {
  const state = engine();
  const commands = useMemo(() => {
    if (!state.offered) return [];
    const setting: PaletteSetting = {
      id: "renderer",
      owner: state.owner,
      label: "Renderer",
      icon: SquareTerminalIcon,
      keywords: ["graphics"],
      current: state.value,
      availability: state.availability,
      reselect: state.reselect,
      choices: [
        { value: "automatic", label: "Automatic (Canvas)" },
        { value: "canvas", label: "Canvas" },
        { value: "vulkan", label: "Vulkan (experimental)", availability: state.vulkanAvailability },
        { value: "webgl", label: "WebGL", availability: HIDDEN },
      ],
      choose: (value) => {
        chosen(value);
        if (!state.pending) {
          engine.setState({ value });
          return;
        }
        return new Promise<SettingOutcome>((resolve, reject) => {
          settleChoice = (outcome) => {
            if (outcome === "changed") engine.setState({ value });
            resolve(outcome);
          };
          failChoice = reject;
        });
      },
    };
    return [settingCommand(setting)];
  }, [state]);
  usePaletteCommands("test-engine", "Display", commands);
  return null;
}

function Harness() {
  const [open, setOpen] = useState(true);
  const [filePath, setFilePath] = useState<string | null>(null);
  return (
    <CommandContextProvider session={null} view="other" workspaceTab={null} filePath={filePath}>
      <button type="button" onClick={() => setOpen(false)}>
        Close from outside
      </button>
      <button type="button" onClick={() => setOpen(true)}>
        Reopen
      </button>
      <button type="button" onClick={() => setFilePath("/other.ts")}>
        Change target
      </button>
      <EngineSetting />
      <QuickPickCommandPalette
        open={open}
        onOpenChange={setOpen}
        onToggleLeftSidebar={vi.fn()}
        onToggleRightSidebar={vi.fn()}
      />
    </CommandContextProvider>
  );
}

function renderPalette() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <Harness />
    </QueryClientProvider>,
  );
}

const input = () => screen.getByRole("combobox");
const type = (text: string) => fireEvent.change(input(), { target: { value: text } });
const press = (key: string) => fireEvent.keyDown(input(), { key });
const heading = () =>
  document.querySelector('[data-slot="quick-pick-heading"]')?.textContent ?? null;
const status = () =>
  document.querySelector('[data-slot="palette-page-status"]')?.textContent ?? null;
const activeOption = () => document.querySelector('[role="option"][aria-selected="true"]');
const option = (name: RegExp) => screen.getByRole("option", { name });
const click = (name: string) => fireEvent.click(screen.getByRole("button", { name, hidden: true }));

async function openEngine(query = "Renderer") {
  type(query);
  await screen.findByRole("option", { name: /Renderer/ });
  press("Enter");
  await waitFor(() => expect(heading()).toBe("Renderer"));
  expect(input()).toHaveFocus();
}

beforeEach(() => {
  engine.setState({
    value: "automatic",
    owner: "app",
    offered: true,
    availability: AVAILABLE,
    vulkanAvailability: AVAILABLE,
    reselect: "skip",
    pending: false,
  });
  chosen.mockReset();
  settleChoice = null;
  failChoice = null;
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("a setting's row", () => {
  it("shows the current value, Automatic with what it resolves to, and opens its values", async () => {
    renderPalette();
    type("Renderer");
    const row = await screen.findByRole("option", { name: /Renderer/ });
    expect(row).toHaveTextContent("Automatic (Canvas)");
    // One row for the setting, whatever its value.
    expect(screen.getAllByRole("option", { name: /Renderer/ })).toHaveLength(1);

    press("Enter");
    await waitFor(() => expect(heading()).toBe("Renderer"));
    expect(screen.getAllByRole("option").map((item) => item.textContent)).toEqual([
      expect.stringContaining("Automatic (Canvas)"),
      expect.stringContaining("Canvas"),
      expect.stringContaining("Vulkan (experimental)"),
    ]);
    // The current value is marked and active; a hidden value isn't listed.
    expect(option(/Automatic \(Canvas\)/)).toHaveTextContent("Current");
    expect(activeOption()).toHaveTextContent("Automatic (Canvas)");
    expect(screen.queryByRole("option", { name: /WebGL/ })).toBeNull();
  });

  it("applies a value and returns to the parent with the row selected, showing the new value", async () => {
    renderPalette();
    await openEngine();
    type("vulkan");
    press("Enter");

    expect(chosen).toHaveBeenCalledWith("vulkan");
    await waitFor(() => expect(heading()).toBeNull());
    expect(input()).toHaveValue("Renderer");
    expect(activeOption()).toHaveTextContent("Renderer");
    expect(activeOption()).toHaveTextContent("Vulkan (experimental)");
    // Focus stays in the query when returning.
    expect(input()).toHaveFocus();
  });

  it("goes back one level on Escape, and on Backspace in an empty query", async () => {
    renderPalette();
    await openEngine();
    press("Escape");
    await waitFor(() => expect(heading()).toBeNull());
    expect(screen.getByRole("combobox")).toHaveValue("Renderer");
    expect(activeOption()).toHaveTextContent("Renderer");

    press("Enter");
    await waitFor(() => expect(heading()).toBe("Renderer"));
    press("Backspace");
    await waitFor(() => expect(heading()).toBeNull());
    expect(chosen).not.toHaveBeenCalled();
  });

  it("goes back without writing when the current value is picked again", async () => {
    renderPalette();
    await openEngine();
    press("Enter");
    await waitFor(() => expect(heading()).toBeNull());
    expect(chosen).not.toHaveBeenCalled();
  });

  it("writes the current value again for a setting whose reselect has an effect", async () => {
    engine.setState({ reselect: "apply" });
    renderPalette();
    await openEngine();
    press("Enter");
    expect(chosen).toHaveBeenCalledWith("automatic");
    await waitFor(() => expect(heading()).toBeNull());
  });
});

describe("searching for a value", () => {
  it("finds the setting by a value's name and opens its page on that value", async () => {
    renderPalette();
    await openEngine("vulkan");
    expect(activeOption()).toHaveTextContent("Vulkan (experimental)");
    press("Enter");
    expect(chosen).toHaveBeenCalledWith("vulkan");
  });

  it("opens on the current value when the query names only the setting", async () => {
    engine.setState({ value: "canvas" });
    renderPalette();
    await openEngine("renderer");
    expect(activeOption()).toHaveTextContent("Canvas");
  });

  it("never names a hidden value, and a greyed one shows why and can't be chosen", async () => {
    engine.setState({ vulkanAvailability: unavailableBecause("Needs WebGL 2") });
    renderPalette();
    type("webgl");
    expect(screen.queryByRole("option", { name: /Renderer/ })).toBeNull();

    await openEngine("vulkan");
    expect(activeOption()).toHaveTextContent("Needs WebGL 2");
    press("Enter");
    expect(chosen).not.toHaveBeenCalled();
    expect(heading()).toBe("Renderer");
  });
});

describe("a page bound to its setting", () => {
  it("leaves, without choosing, when another owner now offers the setting", async () => {
    renderPalette();
    await openEngine();
    act(() => engine.setState({ owner: "composer-2" }));
    await waitFor(() => expect(heading()).toBeNull());
    expect(status()).toBe(SETTING_GONE_NOTICE);
    expect(chosen).not.toHaveBeenCalled();
    // The notice goes once the person types.
    type("x");
    expect(status()).toBeNull();
  });

  it("leaves when the setting is withdrawn or stops being available", async () => {
    renderPalette();
    await openEngine();
    act(() => engine.setState({ offered: false }));
    await waitFor(() => expect(heading()).toBeNull());
    expect(status()).toBe(SETTING_GONE_NOTICE);

    act(() => engine.setState({ offered: true }));
    await openEngine();
    act(() => engine.setState({ availability: unavailableBecause("Rendering is off") }));
    await waitFor(() => expect(heading()).toBeNull());
  });

  it("leaves when the command target changes", async () => {
    renderPalette();
    await openEngine();
    click("Change target");
    await waitFor(() => expect(heading()).toBeNull());
    expect(chosen).not.toHaveBeenCalled();
  });

  it("re-marks the current value when it changes elsewhere while open", async () => {
    renderPalette();
    await openEngine();
    act(() => engine.setState({ value: "canvas" }));
    await waitFor(() => expect(option(/^Canvas/)).toHaveTextContent("Current"));
    expect(option(/Automatic/)).not.toHaveTextContent("Current");
  });
});

describe("a choice that waits on its owner", () => {
  beforeEach(() => engine.setState({ pending: true }));

  it("waits on the page, ignores repeat accepts, then returns once it changed", async () => {
    renderPalette();
    await openEngine("vulkan");
    press("Enter");
    expect(status()).toBe("Changing Renderer…");
    press("Enter");
    expect(chosen).toHaveBeenCalledTimes(1);

    await act(async () => settleChoice!("changed"));
    await waitFor(() => expect(heading()).toBeNull());
    expect(activeOption()).toHaveTextContent("Vulkan (experimental)");
  });

  it("stays on the values with the owner's mark when cancelled", async () => {
    renderPalette();
    await openEngine("vulkan");
    press("Enter");
    await act(async () => settleChoice!("cancelled"));
    expect(heading()).toBe("Renderer");
    expect(status()).toBeNull();
    expect(option(/Automatic/)).toHaveTextContent("Current");
    expect(option(/Vulkan/)).not.toHaveTextContent("Current");
  });

  it("says a failed write failed, and an unconfirmed one to check the value", async () => {
    renderPalette();
    await openEngine("vulkan");
    press("Enter");
    await act(async () => settleChoice!("failed"));
    expect(status()).toBe(settingFailedStatus({ label: "Renderer" } as PaletteSetting));
    expect(option(/Automatic/)).toHaveTextContent("Current");

    press("Enter");
    await act(async () => failChoice!(new Error("bridge gone")));
    expect(status()).toBe(CHOICE_UNCONFIRMED_STATUS);
    expect(heading()).toBe("Renderer");
  });

  it("does nothing when it settles after Back or a close", async () => {
    renderPalette();
    await openEngine("vulkan");
    press("Enter");
    press("Escape");
    await waitFor(() => expect(heading()).toBeNull());
    type("Renderer");
    await act(async () => settleChoice!("changed"));
    expect(input()).toHaveValue("Renderer");

    await openEngine("vulkan");
    press("Enter");
    click("Close from outside");
    await act(async () => settleChoice!("changed"));
    expect(paletteChildPageStore.getState().page).toBeNull();
    click("Reopen");
    expect(heading()).toBeNull();
  });
});
