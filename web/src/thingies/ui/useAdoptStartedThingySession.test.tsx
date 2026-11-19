import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { resetThingyRailRequests, useThingyRailRequest } from "@/lib/thingyRailTabs";
import { adoptStartedThingySession } from "@/thingies/host/thingySession";
import { useAdoptStartedThingySession } from "./useAdoptStartedThingySession";
import type * as ThingySessionModule from "@/thingies/host/thingySession";

vi.mock("@/thingies/host/thingySession", async (importOriginal) => ({
  ...(await importOriginal<typeof ThingySessionModule>()),
  adoptStartedThingySession: vi.fn(async () => true),
}));

function Shell() {
  useAdoptStartedThingySession();
  const navigate = useNavigate();
  return (
    <>
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

function renderShell() {
  render(
    <MemoryRouter initialEntries={["/?prompt=Build&thingy_start=board"]}>
      <Shell />
    </MemoryRouter>,
  );
}

afterEach(() => {
  resetThingyRailRequests();
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

  it("forgets the thingy when New session is left without starting it", () => {
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    expect(adoptStartedThingySession).not.toHaveBeenCalled();
  });
});
