import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import { getGadgetShelf } from "@/gadgets/api/gadgetApi";
import { GadgetSidebarNav } from "./GadgetSidebarNav";
import { renderWithGadgets } from "./testUtils";
import type * as GadgetApiModule from "@/gadgets/api/gadgetApi";

vi.mock("@/gadgets/api/gadgetApi", async (importOriginal) => ({
  ...(await importOriginal<typeof GadgetApiModule>()),
  getGadgetShelf: vi.fn(),
}));

afterEach(() => vi.clearAllMocks());

describe("Gadget in the sidebar", () => {
  it("lists the viewer's pinned gadgets under Gadget, each opening full screen with the sidebar kept", async () => {
    vi.mocked(getGadgetShelf).mockResolvedValue({
      pinned: [
        { id: "board", name: "Launch board" },
        { id: "kpi", name: "KPI story" },
      ],
      hasOwned: true,
    });
    const onNavigate = vi.fn();
    renderWithGadgets(<GadgetSidebarNav active={false} onNavigate={onNavigate} />);

    expect(screen.getByRole("link", { name: "Gadget" })).toHaveAttribute("href", "/gadget");
    const pinned = await screen.findByRole("list", { name: "Pinned gadgets" });
    fireEvent.click(within(pinned).getByRole("link", { name: "KPI story" }));
    expect(onNavigate).toHaveBeenCalled();
    const location = screen.getByLabelText("location");
    expect(location).toHaveTextContent("/gadget/kpi");
    expect(location).toHaveAttribute("data-state", JSON.stringify({ keepSidebar: true }));
  });

  it("marks Gadget active on its pages", () => {
    vi.mocked(getGadgetShelf).mockResolvedValue({ pinned: [], hasOwned: false });
    renderWithGadgets(<GadgetSidebarNav active />, { path: "/gadget" });
    expect(screen.getByRole("link", { name: "Gadget" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("list", { name: "Pinned gadgets" })).toBeNull();
  });

  it("shows nothing while the server has no gadgets", () => {
    renderWithGadgets(<GadgetSidebarNav active={false} />, { info: SERVER_INFO_OFFLINE_FALLBACK });
    expect(screen.queryByRole("link", { name: "Gadget" })).toBeNull();
    expect(getGadgetShelf).not.toHaveBeenCalled();
  });
});
