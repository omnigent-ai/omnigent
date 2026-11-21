import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import type { ServerInfo } from "@/lib/capabilities";
import { resetThingyRailRequests, useThingyRailRequest } from "@/lib/thingyRailTabs";
import { deleteThingy, getThingy, getThingyThingySession } from "@/thingies/api/thingyApi";
import { adoptStartedThingySession } from "@/thingies/host/thingySession";
import {
  noteThingyStartFullScreen,
  rememberUnbuiltThingy,
  resetThingyStarts,
  useAdoptStartedThingySession,
} from "./useAdoptStartedThingySession";
import { THINGIES_INFO, thingyFixture } from "./testUtils";
import type * as ThingyApiModule from "@/thingies/api/thingyApi";
import type * as ThingySessionModule from "@/thingies/host/thingySession";

vi.mock("@/thingies/host/thingySession", async (importOriginal) => ({
  ...(await importOriginal<typeof ThingySessionModule>()),
  adoptStartedThingySession: vi.fn(async () => true),
}));
vi.mock("@/thingies/api/thingyApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ThingyApiModule>()),
  getThingy: vi.fn(),
  getThingyThingySession: vi.fn(),
  deleteThingy: vi.fn(),
}));

function Shell() {
  useAdoptStartedThingySession();
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <>
      <span aria-label="location">{location.pathname + location.search}</span>
      <button type="button" onClick={() => navigate("/c/conv_9?o=1&thingy_start=board")}>
        Created, query kept
      </button>
      <button type="button" onClick={() => navigate("/c/temp:abc")}>
        Creating
      </button>
      <button type="button" onClick={() => navigate("/c/conv_9")}>
        Created
      </button>
      <button type="button" onClick={() => navigate("/tasks")}>
        Elsewhere
      </button>
    </>
  );
}

function renderShell(info: ServerInfo = THINGIES_INFO) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <CapabilitiesProvider info={info}>
        <MemoryRouter initialEntries={["/?prompt=Build&thingy_start=board"]}>
          <Shell />
        </MemoryRouter>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  resetThingyRailRequests();
  resetThingyStarts();
  vi.clearAllMocks();
});

describe("a session started beside a thingy", () => {
  it("shows the thingy in the session once it has its real ID", async () => {
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Creating" }));
    expect(adoptStartedThingySession).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    expect(adoptStartedThingySession).toHaveBeenCalledWith("board", "conv_9");
    await waitFor(() =>
      expect(useThingyRailRequest.getState().requested).toMatchObject({
        conversationId: "conv_9",
        key: "thingy:board",
      }),
    );
  });

  it("keeps a thingy full screen in the session started beside it in full screen", async () => {
    renderShell();
    noteThingyStartFullScreen("board", true);

    fireEvent.click(screen.getByRole("button", { name: "Created" }));

    await waitFor(() =>
      expect(useThingyRailRequest.getState().requested).toEqual({
        conversationId: "conv_9",
        key: "thingy:board",
        maximize: true,
        keepSidebar: true,
      }),
    );
  });

  it("docks a thingy that was docked beside New session", async () => {
    renderShell();
    noteThingyStartFullScreen("board", true);
    noteThingyStartFullScreen("board", false);

    fireEvent.click(screen.getByRole("button", { name: "Created" }));

    await waitFor(() =>
      expect(useThingyRailRequest.getState().requested).toMatchObject({
        conversationId: "conv_9",
        maximize: false,
      }),
    );
  });

  it("adopts the thingy once when the host keeps the query on the session's page", async () => {
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Created, query kept" }));

    await waitFor(() =>
      expect(screen.getByLabelText("location")).toHaveTextContent("/c/conv_9?o=1"),
    );
    expect(screen.getByLabelText("location").textContent).toBe("/c/conv_9?o=1");
    expect(adoptStartedThingySession).toHaveBeenCalledTimes(1);
    expect(adoptStartedThingySession).toHaveBeenCalledWith("board", "conv_9");
  });

  it("forgets the thingy when New session is left without starting it", () => {
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    expect(adoptStartedThingySession).not.toHaveBeenCalled();
  });

  it("deletes a thingy Build made when New session is left without sending it", async () => {
    vi.mocked(getThingy).mockResolvedValue(
      thingyFixture({ id: "board", published_revision: null }),
    );
    vi.mocked(getThingyThingySession).mockResolvedValue(null);
    vi.mocked(deleteThingy).mockResolvedValue(undefined);
    rememberUnbuiltThingy("board");
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    await waitFor(() => expect(deleteThingy).toHaveBeenCalledWith("board"));
  });

  it("keeps a thingy that has a version or a session, or that a session started from", async () => {
    vi.mocked(getThingy).mockResolvedValue(thingyFixture({ id: "board" }));
    rememberUnbuiltThingy("board");
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    await waitFor(() => expect(getThingy).toHaveBeenCalledWith("board"));
    expect(deleteThingy).not.toHaveBeenCalled();

    resetThingyStarts();
    rememberUnbuiltThingy("board");
    renderShell();
    fireEvent.click(screen.getAllByRole("button", { name: "Created" }).at(-1)!);
    await waitFor(() => expect(adoptStartedThingySession).toHaveBeenCalled());
    expect(deleteThingy).not.toHaveBeenCalled();
  });
});

describe("with thingies off", () => {
  it("leaves a thingy_start URL alone and asks for no thingy", () => {
    renderShell({ ...SERVER_INFO_OFFLINE_FALLBACK, features: { thingies: false } });
    fireEvent.click(screen.getByRole("button", { name: "Created, query kept" }));
    expect(screen.getByLabelText("location")).toHaveTextContent("/c/conv_9?o=1&thingy_start=board");
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    expect(adoptStartedThingySession).not.toHaveBeenCalled();
    expect(getThingy).not.toHaveBeenCalled();
    expect(getThingyThingySession).not.toHaveBeenCalled();
    expect(deleteThingy).not.toHaveBeenCalled();
  });
});
