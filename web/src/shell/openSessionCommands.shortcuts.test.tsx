import { cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

import { type OpenSessionFacts, useOpenSessionPaletteCommands } from "./openSessionCommands";

const FACTS: OpenSessionFacts = {
  canAddAgent: true,
  pinnable: true,
  pinned: false,
  pinSaving: false,
  canShare: true,
  canFork: true,
  canMarkUnread: true,
  canCreateAutomation: true,
  archived: false,
  desktopShortcuts: false,
};

function rowShortcuts(quickPick: boolean, facts: OpenSessionFacts) {
  const info = { ...FALLBACK_SERVER_INFO, features: { quick_pick_palette: quickPick } };
  const wrapper = ({ children }: { children: ReactNode }) => (
    <CapabilitiesProvider info={info}>{children}</CapabilitiesProvider>
  );
  renderHook(() => useOpenSessionPaletteCommands({ conversationId: "c1", facts, onRun: vi.fn() }), {
    wrapper,
  });
  const rows = usePaletteCommandsStore.getState().groups["open-session:c1"]?.commands ?? [];
  return Object.fromEntries(
    rows.flatMap((row) => (row.shortcut ? [[row.id.split(":").at(-1), row.shortcut]] : [])),
  );
}

afterEach(() => {
  cleanup();
  usePaletteCommandsStore.setState({ groups: {} });
});

describe("the open session's ⌘K rows", () => {
  it("show the browser's Pin, Fork and Archive keys with QuickPick on", () => {
    expect(rowShortcuts(true, FACTS)).toEqual({
      pin: "session.pin",
      fork: "session.fork",
      archive: "session.archive",
    });
    cleanup();
    expect(rowShortcuts(true, { ...FACTS, pinned: true, archived: true })).toEqual({
      unpin: "session.pin",
      fork: "session.fork",
      unarchive: "session.archive",
    });
  });

  it("keep the desktop menu's own keys, and none with QuickPick off", () => {
    expect(rowShortcuts(true, { ...FACTS, desktopShortcuts: true })).toEqual({});
    cleanup();
    expect(rowShortcuts(false, FACTS)).toEqual({});
  });
});
