import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { SERVER_INFO_OFFLINE_FALLBACK } from "@/lib/bootCapabilities";
import type { ServerInfo } from "@/lib/capabilities";
import { resetGadgetRailRequests, useGadgetRailRequest } from "@/lib/gadgetRailTabs";
import {
  discardUnbuiltGadget,
  getGadget,
  getGadgetGadgetSession,
  recordGadgetActionDelivery,
} from "@/gadgets/api/gadgetApi";
import {
  acknowledgeStartedGadgetInput,
  adoptStartedGadgetSession,
  gadgetSessionStarting,
} from "@/gadgets/host/gadgetSession";
import {
  noteGadgetStartFullScreen,
  rememberUnbuiltGadget,
  resetGadgetStarts,
  useAdoptStartedGadgetSession,
} from "./useAdoptStartedGadgetSession";
import {
  beginSessionCreation,
  noteSessionCreatedHere,
  noteFirstInputAccepted,
  noteSessionCreationFailed,
  resetSessionCreations,
} from "@/lib/sessionCreations";
import { GADGETS_INFO, gadgetFixture } from "./testUtils";
import type * as GadgetApiModule from "@/gadgets/api/gadgetApi";
import type * as GadgetSessionModule from "@/gadgets/host/gadgetSession";

vi.mock("@/gadgets/host/gadgetSession", async (importOriginal) => ({
  ...(await importOriginal<typeof GadgetSessionModule>()),
  adoptStartedGadgetSession: vi.fn(async () => true),
  acknowledgeStartedGadgetInput: vi.fn(async (): Promise<string[]> => []),
}));
vi.mock("@/gadgets/api/gadgetApi", async (importOriginal) => ({
  ...(await importOriginal<typeof GadgetApiModule>()),
  getGadget: vi.fn(),
  getGadgetGadgetSession: vi.fn(),
  discardUnbuiltGadget: vi.fn(),
  recordGadgetActionDelivery: vi.fn(async () => undefined),
}));

// The create the Creating button began, which Created then finishes.
let pendingAttempt: number | null = null;

function finishCreate(sessionId: string, firstMessage: string): void {
  const attempt = pendingAttempt ?? beginSessionCreation();
  pendingAttempt = null;
  noteSessionCreatedHere(sessionId, firstMessage, attempt);
}

function Shell() {
  useAdoptStartedGadgetSession();
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <>
      <span aria-label="location">{location.pathname + location.search}</span>
      <button
        type="button"
        onClick={() => {
          finishCreate("conv_9", "Build");
          navigate("/c/conv_9?o=1&gadget_start=board");
        }}
      >
        Created, query kept
      </button>
      <button
        type="button"
        onClick={() => {
          // Build is sent: New session begins the create and shows the temporary session.
          pendingAttempt = beginSessionCreation();
          navigate("/c/temp:abc");
        }}
      >
        Creating
      </button>
      <button
        type="button"
        onClick={() => {
          // New session creates conv_9, then shows its page.
          finishCreate("conv_9", "Build");
          navigate("/c/conv_9");
        }}
      >
        Created
      </button>
      <button type="button" onClick={() => navigate("/tasks")}>
        Elsewhere
      </button>
      <button type="button" onClick={() => navigate("/c/conv_9")}>
        Session again
      </button>
      <button type="button" onClick={() => navigate("/c/conv_old")}>
        Existing session
      </button>
      <button type="button" onClick={() => navigate("/?prompt=Build&gadget_start=board")}>
        Back to New session
      </button>
    </>
  );
}

function renderShell(info: ServerInfo = GADGETS_INFO) {
  // New session creates conv_9 in these tests; conv_old is a session the viewer already had.
  render(
    <QueryClientProvider client={new QueryClient()}>
      <CapabilitiesProvider info={info}>
        <MemoryRouter initialEntries={["/?prompt=Build&gadget_start=board"]}>
          <Shell />
        </MemoryRouter>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  pendingAttempt = null;
  resetGadgetRailRequests();
  resetGadgetStarts();
  resetSessionCreations();
  vi.clearAllMocks();
});

describe("a session started beside a gadget", () => {
  it("shows the gadget's session as starting from its create until its requests are counted", async () => {
    renderShell();
    expect(gadgetSessionStarting("board")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Creating" }));
    // From Create session on, the gadget's own buttons wait instead of asking again.
    expect(gadgetSessionStarting("board")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    expect(gadgetSessionStarting("board")).toBe(true);
    noteFirstInputAccepted("conv_9", "Build");
    await waitFor(() => expect(gadgetSessionStarting("board")).toBe(false));
  });

  it("stops showing the gadget's session as starting when its create fails", () => {
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Creating" }));
    expect(gadgetSessionStarting("board")).toBe(true);
    noteSessionCreationFailed(pendingAttempt!);
    expect(gadgetSessionStarting("board")).toBe(false);
  });

  it("stops showing the gadget's session as starting when linking it fails", async () => {
    vi.mocked(adoptStartedGadgetSession).mockRejectedValueOnce(new Error("offline"));
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Creating" }));
    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    await waitFor(() => expect(gadgetSessionStarting("board")).toBe(false));
  });

  it("shows the gadget in the session once it has its real ID", async () => {
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Creating" }));
    expect(adoptStartedGadgetSession).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    expect(adoptStartedGadgetSession).toHaveBeenCalledWith("board", "conv_9");
    await waitFor(() =>
      expect(useGadgetRailRequest.getState().requested).toMatchObject({
        conversationId: "conv_9",
        key: "gadget:board",
      }),
    );
  });

  it("counts the gadget's requests delivered only once the session's first message is accepted", async () => {
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Creating" }));
    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    expect(adoptStartedGadgetSession).toHaveBeenCalledWith("board", "conv_9");
    // Created, but its first message is still on its way, was refused or was cancelled:
    // nothing it carried counts as delivered, and Address all's comments stay open.
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(acknowledgeStartedGadgetInput).not.toHaveBeenCalled();

    noteFirstInputAccepted("conv_9", "Build");
    await waitFor(() =>
      expect(acknowledgeStartedGadgetInput).toHaveBeenCalledWith("board", "conv_9", "Build"),
    );
    noteFirstInputAccepted("conv_9", "Build");
    expect(acknowledgeStartedGadgetInput).toHaveBeenCalledTimes(1);
  });

  it("counts requests delivered by the message the server accepted, not the one created with", async () => {
    // The first send failed; the viewer removed the requests and sent something else.
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Creating" }));
    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    noteFirstInputAccepted("conv_9", "Something else");
    await waitFor(() =>
      expect(acknowledgeStartedGadgetInput).toHaveBeenCalledWith(
        "board",
        "conv_9",
        "Something else",
      ),
    );
  });

  it("waits for the gadget's link before counting anything delivered", async () => {
    // The server takes deliveries only for the gadget's session, so an accepted first message
    // waits for the link to finish.
    let link: (() => void) | null = null;
    vi.mocked(adoptStartedGadgetSession).mockImplementationOnce(
      () =>
        new Promise<boolean>((resolve) => {
          link = () => resolve(true);
        }),
    );
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Creating" }));
    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    noteFirstInputAccepted("conv_9", "Build");
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(acknowledgeStartedGadgetInput).not.toHaveBeenCalled();
    expect(link).not.toBeNull();
    link!();
    await waitFor(() =>
      expect(acknowledgeStartedGadgetInput).toHaveBeenCalledWith("board", "conv_9", "Build"),
    );
  });

  it("counts the requests delivered once a link that failed succeeds on the next visit", async () => {
    vi.mocked(adoptStartedGadgetSession).mockRejectedValueOnce(new Error("offline"));
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Creating" }));
    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    noteFirstInputAccepted("conv_9", "Build");
    await waitFor(() => expect(adoptStartedGadgetSession).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(acknowledgeStartedGadgetInput).not.toHaveBeenCalled();

    // The viewer comes back to the session: the link is tried again, and the accepted first
    // message counts once it succeeds.
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    fireEvent.click(screen.getByRole("button", { name: "Session again" }));
    await waitFor(() => expect(adoptStartedGadgetSession).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(acknowledgeStartedGadgetInput).toHaveBeenCalledWith("board", "conv_9", "Build"),
    );
    expect(acknowledgeStartedGadgetInput).toHaveBeenCalledTimes(1);
  });

  it("tries again to record deliveries that failed when the session shows again", async () => {
    vi.mocked(acknowledgeStartedGadgetInput).mockResolvedValueOnce(["act1"]);
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Creating" }));
    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    noteFirstInputAccepted("conv_9", "Build");
    await waitFor(() => expect(acknowledgeStartedGadgetInput).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(recordGadgetActionDelivery).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    fireEvent.click(screen.getByRole("button", { name: "Session again" }));
    await waitFor(() =>
      expect(recordGadgetActionDelivery).toHaveBeenCalledWith("board", "act1", "conv_9"),
    );
  });

  it("keeps a gadget full screen in the session started beside it in full screen", async () => {
    renderShell();
    noteGadgetStartFullScreen("board", true);

    fireEvent.click(screen.getByRole("button", { name: "Created" }));

    await waitFor(() =>
      expect(useGadgetRailRequest.getState().requested).toEqual({
        conversationId: "conv_9",
        key: "gadget:board",
        maximize: true,
        keepSidebar: true,
      }),
    );
  });

  it("docks a gadget that was docked beside New session", async () => {
    renderShell();
    noteGadgetStartFullScreen("board", true);
    noteGadgetStartFullScreen("board", false);

    fireEvent.click(screen.getByRole("button", { name: "Created" }));

    await waitFor(() =>
      expect(useGadgetRailRequest.getState().requested).toMatchObject({
        conversationId: "conv_9",
        maximize: false,
      }),
    );
  });

  it("adopts the gadget once when the host keeps the query on the session's page", async () => {
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Created, query kept" }));

    await waitFor(() =>
      expect(screen.getByLabelText("location")).toHaveTextContent("/c/conv_9?o=1"),
    );
    expect(screen.getByLabelText("location").textContent).toBe("/c/conv_9?o=1");
    expect(adoptStartedGadgetSession).toHaveBeenCalledTimes(1);
    expect(adoptStartedGadgetSession).toHaveBeenCalledWith("board", "conv_9");
  });

  it("forgets the gadget when New session is left without starting it", () => {
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    expect(adoptStartedGadgetSession).not.toHaveBeenCalled();
  });

  it("doesn't take an existing session the viewer opens instead for the gadget's", async () => {
    vi.mocked(getGadget).mockResolvedValue(
      gadgetFixture({ id: "board", published_revision: null }),
    );
    vi.mocked(getGadgetGadgetSession).mockResolvedValue(null);
    vi.mocked(discardUnbuiltGadget).mockResolvedValue(true);
    rememberUnbuiltGadget("board");
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Existing session" }));
    // Not linked, its requests not acknowledged, and Build's unbuilt gadget goes as if New
    // session were left.
    expect(adoptStartedGadgetSession).not.toHaveBeenCalled();
    await waitFor(() => expect(discardUnbuiltGadget).toHaveBeenCalledWith("board"));
    // A session created later from a plain New session isn't the gadget's either.
    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    expect(adoptStartedGadgetSession).not.toHaveBeenCalled();
  });

  it("doesn't take a session this tab created before the gadget's New session opened", async () => {
    vi.mocked(getGadget).mockResolvedValue(
      gadgetFixture({ id: "board", published_revision: null }),
    );
    vi.mocked(getGadgetGadgetSession).mockResolvedValue(null);
    vi.mocked(discardUnbuiltGadget).mockResolvedValue(true);
    noteSessionCreatedHere("conv_old", "Earlier work");
    rememberUnbuiltGadget("board");
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Existing session" }));
    expect(adoptStartedGadgetSession).not.toHaveBeenCalled();
    await waitFor(() => expect(discardUnbuiltGadget).toHaveBeenCalledWith("board"));
  });

  it("keeps and links the gadget when Build was sent and the viewer left before the session existed", async () => {
    rememberUnbuiltGadget("board");
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Creating" }));
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    await Promise.resolve();
    expect(getGadget).not.toHaveBeenCalled();
    expect(discardUnbuiltGadget).not.toHaveBeenCalled();

    // The create finishes while the viewer is on another page.
    finishCreate("conv_9", "Build");
    expect(adoptStartedGadgetSession).toHaveBeenCalledWith("board", "conv_9");
    await waitFor(() =>
      expect(useGadgetRailRequest.getState().requested).toMatchObject({
        conversationId: "conv_9",
        key: "gadget:board",
      }),
    );
  });

  it("doesn't take a session another create makes after the gadget's create failed", async () => {
    vi.mocked(getGadget).mockResolvedValue(
      gadgetFixture({ id: "board", published_revision: null }),
    );
    vi.mocked(getGadgetGadgetSession).mockResolvedValue(null);
    vi.mocked(discardUnbuiltGadget).mockResolvedValue(true);
    rememberUnbuiltGadget("board");
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Creating" }));
    // The create fails (a runner that didn't launch, say): the draft goes back to the viewer.
    noteSessionCreationFailed(pendingAttempt!);
    pendingAttempt = null;
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    // A plain session the viewer creates later is not the gadget's, and Build's gadget goes.
    noteSessionCreatedHere("conv_plain", "Other work", beginSessionCreation());
    expect(adoptStartedGadgetSession).not.toHaveBeenCalled();
    await waitFor(() => expect(discardUnbuiltGadget).toHaveBeenCalledWith("board"));
  });

  it("never deletes an existing gadget whose Create session failed", async () => {
    // An unpublished gadget the viewer already had, with no session: it was never made to be
    // built here, so a failed create mustn't mark it for deletion.
    vi.mocked(getGadget).mockResolvedValue(
      gadgetFixture({ id: "board", published_revision: null }),
    );
    vi.mocked(getGadgetGadgetSession).mockResolvedValue(null);
    vi.mocked(discardUnbuiltGadget).mockResolvedValue(true);
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Creating" }));
    noteSessionCreationFailed(pendingAttempt!);
    pendingAttempt = null;
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    await Promise.resolve();
    await Promise.resolve();
    expect(discardUnbuiltGadget).not.toHaveBeenCalled();
  });

  it("links the session its own create makes, when another create finishes first", () => {
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Creating" }));
    const gadgets = pendingAttempt!;
    pendingAttempt = null;
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    const other = beginSessionCreation();
    noteSessionCreatedHere("conv_other", "Other work", other);
    expect(adoptStartedGadgetSession).not.toHaveBeenCalled();
    noteSessionCreatedHere("conv_9", "Build", gadgets);
    expect(adoptStartedGadgetSession).toHaveBeenCalledTimes(1);
    expect(adoptStartedGadgetSession).toHaveBeenCalledWith("board", "conv_9");
  });

  it("links the gadget again when its session shows after a link that failed", async () => {
    vi.mocked(adoptStartedGadgetSession).mockRejectedValueOnce(new Error("offline"));
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    expect(adoptStartedGadgetSession).toHaveBeenCalledTimes(1);
    await Promise.resolve();
    await Promise.resolve();
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    fireEvent.click(screen.getByRole("button", { name: "Created, query kept" }));
    await waitFor(() => expect(adoptStartedGadgetSession).toHaveBeenCalledTimes(2));
    expect(adoptStartedGadgetSession).toHaveBeenLastCalledWith("board", "conv_9");
  });

  it("deletes a gadget Build made when New session is left without sending it", async () => {
    vi.mocked(getGadget).mockResolvedValue(
      gadgetFixture({ id: "board", published_revision: null }),
    );
    vi.mocked(getGadgetGadgetSession).mockResolvedValue(null);
    vi.mocked(discardUnbuiltGadget).mockResolvedValue(true);
    rememberUnbuiltGadget("board");
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    await waitFor(() => expect(discardUnbuiltGadget).toHaveBeenCalledWith("board"));
  });

  it("keeps the gadget when the viewer returns and sends Build while the discard is waiting", async () => {
    vi.mocked(getGadget).mockResolvedValue(
      gadgetFixture({ id: "board", published_revision: null }),
    );
    let answerSessionLookup: (session: string | null) => void = () => {};
    vi.mocked(getGadgetGadgetSession).mockReturnValue(
      new Promise<string | null>((resolve) => {
        answerSessionLookup = resolve;
      }),
    );
    vi.mocked(discardUnbuiltGadget).mockResolvedValue(true);
    rememberUnbuiltGadget("board");
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    await waitFor(() => expect(getGadgetGadgetSession).toHaveBeenCalledWith("board"));
    fireEvent.click(screen.getByRole("button", { name: "Back to New session" }));
    fireEvent.click(screen.getByRole("button", { name: "Creating" }));
    // The lookup made before the viewer came back answers "no session" late.
    answerSessionLookup(null);
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(discardUnbuiltGadget).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Created" }));
    await waitFor(() => expect(adoptStartedGadgetSession).toHaveBeenCalled());
    expect(discardUnbuiltGadget).not.toHaveBeenCalled();
  });

  it("keeps a gadget that has a version or a session, or that a session started from", async () => {
    vi.mocked(getGadget).mockResolvedValue(gadgetFixture({ id: "board" }));
    rememberUnbuiltGadget("board");
    renderShell();
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    await waitFor(() => expect(getGadget).toHaveBeenCalledWith("board"));
    expect(discardUnbuiltGadget).not.toHaveBeenCalled();

    // Unpublished, but it already has a gadget session.
    resetGadgetStarts();
    vi.mocked(getGadget).mockResolvedValue(
      gadgetFixture({ id: "board", published_revision: null }),
    );
    vi.mocked(getGadgetGadgetSession).mockResolvedValue("conv_7");
    rememberUnbuiltGadget("board");
    renderShell();
    fireEvent.click(screen.getAllByRole("button", { name: "Elsewhere" }).at(-1)!);
    await waitFor(() => expect(getGadgetGadgetSession).toHaveBeenCalledWith("board"));
    expect(discardUnbuiltGadget).not.toHaveBeenCalled();

    resetGadgetStarts();
    rememberUnbuiltGadget("board");
    renderShell();
    fireEvent.click(screen.getAllByRole("button", { name: "Created" }).at(-1)!);
    await waitFor(() => expect(adoptStartedGadgetSession).toHaveBeenCalled());
    expect(discardUnbuiltGadget).not.toHaveBeenCalled();
  });
});

describe("with gadgets off", () => {
  it("leaves a gadget_start URL alone and asks for no gadget", () => {
    renderShell({ ...SERVER_INFO_OFFLINE_FALLBACK, features: { gadgets: false } });
    fireEvent.click(screen.getByRole("button", { name: "Created, query kept" }));
    expect(screen.getByLabelText("location")).toHaveTextContent("/c/conv_9?o=1&gadget_start=board");
    fireEvent.click(screen.getByRole("button", { name: "Elsewhere" }));
    expect(adoptStartedGadgetSession).not.toHaveBeenCalled();
    expect(getGadget).not.toHaveBeenCalled();
    expect(getGadgetGadgetSession).not.toHaveBeenCalled();
    expect(discardUnbuiltGadget).not.toHaveBeenCalled();
  });
});
