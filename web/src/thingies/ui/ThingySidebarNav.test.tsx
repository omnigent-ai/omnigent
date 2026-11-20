import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import { getThingyShelf } from "@/thingies/api/thingyApi";
import { ThingySidebarNav } from "./ThingySidebarNav";
import { renderWithThingies } from "./testUtils";
import type * as ThingyApiModule from "@/thingies/api/thingyApi";

vi.mock("@/thingies/api/thingyApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ThingyApiModule>()),
  getThingyShelf: vi.fn(),
}));

afterEach(() => vi.clearAllMocks());

describe("Thingy in the sidebar", () => {
  it("lists the viewer's pinned thingies under Thingy, each opening full screen with the sidebar kept", async () => {
    vi.mocked(getThingyShelf).mockResolvedValue({
      pinned: [
        { id: "board", name: "Launch board" },
        { id: "kpi", name: "KPI story" },
      ],
      hasOwned: true,
    });
    const onNavigate = vi.fn();
    renderWithThingies(<ThingySidebarNav active={false} onNavigate={onNavigate} />);

    expect(screen.getByRole("link", { name: "Thingy" })).toHaveAttribute("href", "/thingy");
    const pinned = await screen.findByRole("list", { name: "Pinned thingies" });
    fireEvent.click(within(pinned).getByRole("link", { name: "KPI story" }));
    expect(onNavigate).toHaveBeenCalled();
    const location = screen.getByLabelText("location");
    expect(location).toHaveTextContent("/thingy/kpi");
    expect(location).toHaveAttribute("data-state", JSON.stringify({ keepSidebar: true }));
  });

  it("marks Thingy active on its pages", () => {
    vi.mocked(getThingyShelf).mockResolvedValue({ pinned: [], hasOwned: false });
    renderWithThingies(<ThingySidebarNav active />, { path: "/thingy" });
    expect(screen.getByRole("link", { name: "Thingy" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("list", { name: "Pinned thingies" })).toBeNull();
  });

  it("shows nothing while the server has no thingies", () => {
    renderWithThingies(<ThingySidebarNav active={false} />, { info: SERVER_INFO_OFFLINE_FALLBACK });
    expect(screen.queryByRole("link", { name: "Thingy" })).toBeNull();
    expect(getThingyShelf).not.toHaveBeenCalled();
  });
});
