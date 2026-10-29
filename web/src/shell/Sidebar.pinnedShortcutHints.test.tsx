import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ALT_KEY, ARIA_MOD_KEY, MOD_KEY } from "@/components/KeyboardShortcut";
import { useConversations } from "@/hooks/useConversations";
import { COMMAND_HOLD_REVEAL_DELAY_MS, useCommandHoldStore } from "@/hooks/useCommandHold";
import { isMacPlatform } from "@/lib/hotkeys";
import { PINNED_LABEL_KEY } from "@/lib/sessionListCache";
import { conversation as conv, conversationPage } from "@/test/sidebarMockHelpers";
import { renderSidebar } from "@/test/sidebarTestHelpers";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

const mocks = vi.hoisted(() => ({
  pinned: [] as ReturnType<typeof conv>[],
  mobile: false,
}));

vi.mock("@/hooks/useIsMobileViewport", () => ({
  useIsMobileViewport: () => mocks.mobile,
}));

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    usePinnedConversations: () => ({
      data: { conversations: mocks.pinned, filterHonored: true },
      isSuccess: true,
    }),
  };
});

const COMMAND_KEY = isMacPlatform() ? "Meta" : "Control";
const COMMAND_MODIFIER = isMacPlatform() ? { metaKey: true } : { ctrlKey: true };
const ELEVEN_PINNED_IDS = Array.from({ length: 11 }, (_, index) => `conv_${index + 1}`);

/** Pinned fixtures ordered by their pin label, so the first id gets slot 1. */
function pinnedFixtures(ids: string[]) {
  return ids.map((id, index) => conv(id, { labels: { [PINNED_LABEL_KEY]: String(1000 + index) } }));
}

function hints() {
  return document.querySelectorAll<HTMLElement>("[data-pinned-shortcut-hint]");
}

function rowOf(id: string): HTMLElement {
  const row = document.querySelector<HTMLElement>(`li[data-sidebar-session-id="${id}"]`);
  if (!row) throw new Error(`no sidebar row for ${id}`);
  return row;
}

/** The chip text of a row's hint, one entry per key cap. */
function hintKeys(row: HTMLElement): (string | null)[] {
  return Array.from(
    row.querySelectorAll('[data-pinned-shortcut-hint] [data-slot="kbd"]'),
    (keyCap) => keyCap.textContent,
  );
}

function pressCommand() {
  fireEvent.keyDown(window, { key: COMMAND_KEY, ...COMMAND_MODIFIER });
}

function releaseCommand() {
  fireEvent.keyUp(window, { key: COMMAND_KEY });
}

async function waitForHints(count: number) {
  await waitFor(() => expect(hints()).toHaveLength(count));
}

/** Waits past the reveal delay, so a held key would have shown its hints by now. */
async function waitPastRevealDelay() {
  await act(async () => {
    await new Promise((resolve) => {
      setTimeout(resolve, COMMAND_HOLD_REVEAL_DELAY_MS + 100);
    });
  });
}

beforeEach(() => {
  localStorage.clear();
  mocks.mobile = false;
  mocks.pinned = pinnedFixtures(["conv_a", "conv_b", "conv_c"]);
  vi.mocked(useConversations).mockImplementation(() =>
    conversationPage([conv("conv_x"), conv("conv_y")]),
  );
  useCommandHoldStore.setState({ held: false });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Sidebar pinned-row shortcut hints", () => {
  it("shows a chord hint on each pinned row only while the command key is held", async () => {
    renderSidebar();
    expect(hints()).toHaveLength(0);

    pressCommand();
    expect(hints()).toHaveLength(0); // still inside the reveal delay

    await waitForHints(3);
    expect(hintKeys(rowOf("conv_a"))).toEqual([MOD_KEY, ALT_KEY, "1"]);
    expect(hintKeys(rowOf("conv_b"))).toEqual([MOD_KEY, ALT_KEY, "2"]);
    expect(hintKeys(rowOf("conv_c"))).toEqual([MOD_KEY, ALT_KEY, "3"]);
    expect(rowOf("conv_x").querySelector("[data-pinned-shortcut-hint]")).toBeNull();

    releaseCommand();
    expect(hints()).toHaveLength(0);
  });

  it("labels the desktop app's chord without Alt", async () => {
    vi.stubGlobal("omnigentDesktop", { kind: "electron" });
    renderSidebar();

    pressCommand();
    await waitForHints(3);
    expect(hintKeys(rowOf("conv_a"))).toEqual([MOD_KEY, "1"]);
    expect(hintKeys(rowOf("conv_c"))).toEqual([MOD_KEY, "3"]);
  });

  it("labels the tenth pinned row 0 and gives no hint to an eleventh", async () => {
    mocks.pinned = pinnedFixtures(ELEVEN_PINNED_IDS);
    renderSidebar();

    pressCommand();
    await waitForHints(10);
    expect(hintKeys(rowOf("conv_10"))).toEqual([MOD_KEY, ALT_KEY, "0"]);
    expect(rowOf("conv_11").querySelector("[data-pinned-shortcut-hint]")).toBeNull();
  });

  it("exposes the chord to assistive tech on pinned links", () => {
    renderSidebar();

    expect(rowOf("conv_a").querySelector("a")).toHaveAttribute(
      "aria-keyshortcuts",
      `${ARIA_MOD_KEY}+Alt+1`,
    );
    expect(rowOf("conv_x").querySelector("a")).not.toHaveAttribute("aria-keyshortcuts");
  });

  it("hides the hints on window blur and when a digit is pressed", async () => {
    renderSidebar();
    pressCommand();
    await waitForHints(3);

    fireEvent.blur(window);
    expect(hints()).toHaveLength(0);

    pressCommand();
    await waitForHints(3);
    fireEvent.keyDown(window, { key: "1", ...COMMAND_MODIFIER });
    expect(hints()).toHaveLength(0);
  });

  it("shows no hints while the Pinned section is collapsed", async () => {
    renderSidebar();
    fireEvent.click(screen.getByRole("button", { name: "Pinned" }));

    pressCommand();
    await waitPastRevealDelay();
    expect(useCommandHoldStore.getState().held).toBe(true);
    expect(hints()).toHaveLength(0);
    releaseCommand();

    fireEvent.click(screen.getByRole("button", { name: "Pinned" }));
    pressCommand();
    await waitForHints(3);
    releaseCommand();
  });

  it("shows no hints on touch layouts", async () => {
    mocks.mobile = true;
    renderSidebar();
    expect(rowOf("conv_a")).toBeTruthy();

    pressCommand();
    await waitPastRevealDelay();
    expect(useCommandHoldStore.getState().held).toBe(true);
    expect(hints()).toHaveLength(0);
    releaseCommand();
  });
});
