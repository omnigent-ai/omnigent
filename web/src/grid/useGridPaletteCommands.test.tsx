import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

import type * as GridSessionsModule from "./GridSessionsContext";
import type { SessionGrid } from "./gridLayout";
import { GridSidebar } from "./GridWorkspace";

const OPEN: SessionGrid = {
  id: "g1",
  name: "Review",
  layout: "s1",
};
const OTHER: SessionGrid = {
  id: "g2",
  name: "Deploys",
  layout: "s2",
};
const grids = vi.hoisted(() => ({ renameGrid: vi.fn(), deleteGrid: vi.fn() }));
vi.mock("./GridSessionsContext", async (importOriginal) => ({
  ...(await importOriginal<typeof GridSessionsModule>()),
  useGridSessions: () => ({ enabled: true, grids: [OPEN, OTHER], activeGrid: OPEN, ...grids }),
}));
vi.mock("@/hooks/useSidebarData", () => ({ useLoadedConversations: () => ({ data: undefined }) }));

function renderSidebar(flag = true) {
  render(
    <CapabilitiesProvider
      info={{ ...FALLBACK_SERVER_INFO, features: { quick_pick_palette: flag } }}
    >
      <MemoryRouter>
        <GridSidebar />
      </MemoryRouter>
    </CapabilitiesProvider>,
  );
}
const commands = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  usePaletteCommandsStore.setState({ groups: {} });
});

describe("the open grid's actions in ⌘K", () => {
  it("renames and deletes the open grid only, as its row menu does", () => {
    renderSidebar();
    expect(commands().map(({ label, description }) => [label, description])).toEqual([
      ["Rename grid…", "Review"],
      ["Delete grid", "Review"],
    ]);
    act(() => commands()[0]!.run());
    const field = screen.getByRole("textbox");
    fireEvent.change(field, { target: { value: "Morning review" } });
    fireEvent.keyDown(field, { key: "Enter" });
    expect(grids.renameGrid).toHaveBeenCalledWith("g1", "Morning review");
    act(() =>
      commands()
        .find(({ label }) => label === "Delete grid")!
        .run(),
    );
    expect(grids.deleteGrid).toHaveBeenCalledWith("g1");
  });

  it("offers nothing with quick_pick_palette off", () => {
    renderSidebar(false);
    expect(commands()).toEqual([]);
  });
});
