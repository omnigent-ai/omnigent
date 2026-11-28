import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { Link, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GadgetApiError, getGadget, getGadgetShelf } from "@/gadgets/api/gadgetApi";
import { openGadgetFullScreen } from "@/gadgets/host/gadgetSession";
import { GadgetOpenPage } from "./GadgetOpenPage";
import { renderWithGadgets, gadgetFixture } from "./testUtils";
import type * as GadgetSessionModule from "@/gadgets/host/gadgetSession";
import type * as GadgetApiModule from "@/gadgets/api/gadgetApi";

vi.mock("@/gadgets/api/gadgetApi", async (importOriginal) => ({
  ...(await importOriginal<typeof GadgetApiModule>()),
  getGadget: vi.fn(),
  getGadgetShelf: vi.fn(),
}));
vi.mock("@/gadgets/host/gadgetSession", async (importOriginal) => ({
  ...(await importOriginal<typeof GadgetSessionModule>()),
  openGadgetFullScreen: vi.fn(),
}));

const BOARD = gadgetFixture({ id: "board" });

/** Shows the link state the session's page got. */
function StateProbe() {
  const { state } = useLocation();
  return (
    <span aria-label="keep sidebar">
      {String((state as { keepSidebar?: boolean } | null)?.keepSidebar)}
    </span>
  );
}

afterEach(() => vi.clearAllMocks());

describe("a gadget's link", () => {
  it("opens the gadget full screen in the viewer's gadget session", async () => {
    vi.mocked(getGadget).mockResolvedValue(BOARD);
    vi.mocked(openGadgetFullScreen).mockImplementation(async (_gadget, navigate) =>
      navigate("/c/conv_7"),
    );
    renderWithGadgets(<GadgetOpenPage />, { path: "/gadget/board", route: "/gadget/:gadgetId" });

    // The session's URL names the gadget filling the screen, so Forward opens it the same way.
    await waitFor(() =>
      expect(screen.getByLabelText("location")).toHaveTextContent("/c/conv_7?gadget=board"),
    );
    expect(openGadgetFullScreen).toHaveBeenCalledWith(BOARD, expect.any(Function), {
      keepSidebar: false,
    });
  });

  it("leaves the sidebar as it was when opened from inside Omnigent", async () => {
    vi.mocked(getGadget).mockResolvedValue(BOARD);
    let navigate: ((url: string) => void) | undefined;
    vi.mocked(openGadgetFullScreen).mockImplementation(async (_gadget, go) => {
      navigate = go;
    });
    renderWithGadgets(
      <Routes>
        <Route path="/" element={<Link to="/gadget/board">Board</Link>} />
        <Route path="/gadget/:gadgetId" element={<GadgetOpenPage />} />
        <Route path="*" element={<StateProbe />} />
      </Routes>,
    );

    fireEvent.click(screen.getByRole("link", { name: "Board" }));
    await waitFor(() =>
      expect(openGadgetFullScreen).toHaveBeenCalledWith(BOARD, expect.any(Function), {
        keepSidebar: true,
      }),
    );
    // The session's page is a new entry, so it carries the decision on.
    act(() => navigate!("/c/conv_7"));
    expect(screen.getByLabelText("keep sidebar")).toHaveTextContent("true");
  });

  it("tells the session's page to close the sidebar for a link opened fresh", async () => {
    vi.mocked(getGadget).mockResolvedValue(BOARD);
    vi.mocked(openGadgetFullScreen).mockImplementation(async (_gadget, go) => go("/c/conv_7"));
    renderWithGadgets(
      <Routes>
        <Route path="/gadget/:gadgetId" element={<GadgetOpenPage />} />
        <Route path="*" element={<StateProbe />} />
      </Routes>,
      { path: "/gadget/board" },
    );

    expect(await screen.findByLabelText("keep sidebar")).toHaveTextContent("false");
  });

  it("keeps the sidebar open for a pinned row", async () => {
    vi.mocked(getGadget).mockResolvedValue(BOARD);
    vi.mocked(openGadgetFullScreen).mockResolvedValue(undefined);
    renderWithGadgets(<GadgetOpenPage />, {
      path: { pathname: "/gadget/board", state: { keepSidebar: true } },
      route: "/gadget/:gadgetId",
    });
    await waitFor(() =>
      expect(openGadgetFullScreen).toHaveBeenCalledWith(BOARD, expect.any(Function), {
        keepSidebar: true,
      }),
    );
  });

  it("explains a gadget it can't open, with a way back", async () => {
    vi.mocked(getGadget).mockRejectedValue(new Error("This gadget was deleted."));
    renderWithGadgets(<GadgetOpenPage />, { path: "/gadget/gone", route: "/gadget/:gadgetId" });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn’t open the gadget");
    expect(alert).toHaveTextContent("This gadget was deleted.");
    fireEvent.click(screen.getByRole("link", { name: "Back to your gadgets" }));
    expect(screen.getByLabelText("location")).toHaveTextContent("/gadget");
    expect(openGadgetFullScreen).not.toHaveBeenCalled();
  });

  it("offers Retry when opening it fails", async () => {
    vi.mocked(getGadget).mockResolvedValue(BOARD);
    vi.mocked(openGadgetFullScreen)
      .mockRejectedValueOnce(new Error("Network error"))
      .mockResolvedValueOnce(undefined);
    renderWithGadgets(<GadgetOpenPage />, { path: "/gadget/board", route: "/gadget/:gadgetId" });

    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(openGadgetFullScreen).toHaveBeenCalledTimes(2));
  });

  it("says a deleted or unshared gadget isn't available, without retrying, and offers Unpin", async () => {
    vi.mocked(getGadget).mockRejectedValue(
      new GadgetApiError({ status: 404, code: "NOT_FOUND", message: "Gadget not found" }),
    );
    vi.mocked(getGadgetShelf).mockResolvedValue({
      pinned: [{ id: "gone", name: "Old board" }],
      hasOwned: true,
    });
    renderWithGadgets(<GadgetOpenPage />, { path: "/gadget/gone", route: "/gadget/:gadgetId" });

    expect(await screen.findByText("“Old board” isn’t available")).toBeInTheDocument();
    expect(
      screen.getByText(/It was deleted, or you no longer have access to it/),
    ).toBeInTheDocument();
    expect(getGadget).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.getByRole("button", { name: "Unpin from sidebar" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to your gadgets" })).toBeInTheDocument();
  });
});
