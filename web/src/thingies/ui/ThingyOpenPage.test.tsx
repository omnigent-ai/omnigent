import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getThingy } from "@/thingies/api/thingyApi";
import { openThingyFullScreen } from "@/thingies/host/thingySession";
import { ThingyOpenPage } from "./ThingyOpenPage";
import { renderWithThingies, thingyFixture } from "./testUtils";
import type * as ThingySessionModule from "@/thingies/host/thingySession";
import type * as ThingyApiModule from "@/thingies/api/thingyApi";

vi.mock("@/thingies/api/thingyApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ThingyApiModule>()),
  getThingy: vi.fn(),
}));
vi.mock("@/thingies/host/thingySession", async (importOriginal) => ({
  ...(await importOriginal<typeof ThingySessionModule>()),
  openThingyFullScreen: vi.fn(),
}));

const BOARD = thingyFixture({ id: "board" });

afterEach(() => vi.clearAllMocks());

describe("a thingy's link", () => {
  it("opens the thingy full screen in the viewer's thingy session", async () => {
    vi.mocked(getThingy).mockResolvedValue(BOARD);
    vi.mocked(openThingyFullScreen).mockImplementation(async (_thingy, navigate) =>
      navigate("/c/conv_7"),
    );
    renderWithThingies(<ThingyOpenPage />, { path: "/thingy/board", route: "/thingy/:thingyId" });

    await waitFor(() => expect(screen.getByLabelText("location")).toHaveTextContent("/c/conv_7"));
    expect(openThingyFullScreen).toHaveBeenCalledWith(BOARD, expect.any(Function), {
      keepSidebar: false,
    });
  });

  it("keeps the sidebar open for a pinned row", async () => {
    vi.mocked(getThingy).mockResolvedValue(BOARD);
    vi.mocked(openThingyFullScreen).mockResolvedValue(undefined);
    renderWithThingies(<ThingyOpenPage />, {
      path: { pathname: "/thingy/board", state: { keepSidebar: true } },
      route: "/thingy/:thingyId",
    });
    await waitFor(() =>
      expect(openThingyFullScreen).toHaveBeenCalledWith(BOARD, expect.any(Function), {
        keepSidebar: true,
      }),
    );
  });

  it("explains a thingy it can't open, with a way back", async () => {
    vi.mocked(getThingy).mockRejectedValue(new Error("This thingy was deleted."));
    renderWithThingies(<ThingyOpenPage />, { path: "/thingy/gone", route: "/thingy/:thingyId" });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn’t open the thingy");
    expect(alert).toHaveTextContent("This thingy was deleted.");
    fireEvent.click(screen.getByRole("link", { name: "Back to your thingies" }));
    expect(screen.getByLabelText("location")).toHaveTextContent("/thingy");
    expect(openThingyFullScreen).not.toHaveBeenCalled();
  });

  it("offers Retry when opening it fails", async () => {
    vi.mocked(getThingy).mockResolvedValue(BOARD);
    vi.mocked(openThingyFullScreen)
      .mockRejectedValueOnce(new Error("Network error"))
      .mockResolvedValueOnce(undefined);
    renderWithThingies(<ThingyOpenPage />, { path: "/thingy/board", route: "/thingy/:thingyId" });

    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(openThingyFullScreen).toHaveBeenCalledTimes(2));
  });
});
