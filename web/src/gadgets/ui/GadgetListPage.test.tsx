import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  deleteGadget,
  getGadgetShelf,
  listSharedGadgets,
  listGadgets,
  pinGadget,
} from "@/gadgets/api/gadgetApi";
import { GadgetListPage } from "./GadgetListPage";
import { GADGETS_INFO, renderWithGadgets, gadgetFixture } from "./testUtils";
import type * as GadgetApiModule from "@/gadgets/api/gadgetApi";

vi.mock("@/gadgets/api/gadgetApi", async (importOriginal) => ({
  ...(await importOriginal<typeof GadgetApiModule>()),
  listGadgets: vi.fn(),
  listSharedGadgets: vi.fn(),
  deleteGadget: vi.fn(),
  getGadgetShelf: vi.fn(),
  pinGadget: vi.fn(),
}));
// The template thumbnails run real gadgets; their frame has its own tests.
vi.mock("@/gadgets/host/GadgetFrame", () => ({ GadgetFrame: () => null }));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

const BOARD = gadgetFixture({ id: "board", name: "Launch board" });
const DRAFT = gadgetFixture({
  id: "draft",
  name: "Reading list",
  published_revision: null,
  permissions: { read: true, write_data: true, publish: true, manage: false },
});

beforeEach(() => {
  vi.mocked(getGadgetShelf).mockResolvedValue({ pinned: [], hasOwned: true });
  vi.mocked(listGadgets).mockResolvedValue([BOARD, DRAFT]);
  vi.mocked(listSharedGadgets).mockResolvedValue([
    {
      ...gadgetFixture({ id: "team", name: "Team tracker" }),
      shared_via: "group",
      owner_email: "lee@example.com",
    },
    { ...gadgetFixture({ id: "kpi", name: "KPI story" }), shared_via: "user", owner_email: null },
  ]);
});

afterEach(() => vi.clearAllMocks());

describe("Gadget page", () => {
  it("lists the viewer's gadgets and those shared with them, and opens one", async () => {
    renderWithGadgets(<GadgetListPage />, { path: "/gadget" });

    const yours = await screen.findByTestId("gadgets-list");
    expect(within(yours).getByRole("button", { name: "Open Launch board" })).toBeInTheDocument();
    expect(within(yours).getByText("Not published yet")).toBeInTheDocument();

    const shared = await screen.findByTestId("shared-gadgets-list");
    expect(
      within(shared).getByText("Shared by lee@example.com with a group you’re in"),
    ).toBeInTheDocument();
    expect(within(shared).getByText("Shared by someone")).toBeInTheDocument();
    // Newest first: the server lists them oldest first.
    expect(
      within(yours)
        .getAllByRole("button", { name: /^Open / })
        .map((button) => button.getAttribute("aria-label")),
    ).toEqual(["Open Reading list", "Open Launch board"]);
    expect(within(yours).getByText("Published")).toBeInTheDocument();
    // Only a gadget's managers delete it, and nobody changes the shared list's.
    expect(screen.queryByRole("button", { name: "Actions for Team tracker" })).toBeNull();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Actions for Reading list" }), {
      button: 0,
    });
    expect(await screen.findByRole("menuitem", { name: "Rename" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Delete gadget" })).toBeNull();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });

    fireEvent.click(within(yours).getByRole("button", { name: "Open Launch board" }));
    expect(screen.getByLabelText("location")).toHaveTextContent("/gadget/board");
    // Opened from inside Omnigent, whatever key the host's router gives the entry.
    expect(screen.getByLabelText("location")).toHaveAttribute(
      "data-state",
      JSON.stringify({ keepSidebar: true }),
    );
  });

  it("deletes a gadget after confirming", async () => {
    vi.mocked(deleteGadget).mockResolvedValue(undefined);
    renderWithGadgets(<GadgetListPage />, { path: "/gadget" });

    const menu = await screen.findByRole("button", { name: "Actions for Launch board" });
    fireEvent.pointerDown(menu, { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete gadget" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete gadget?" });
    expect(dialog).toHaveTextContent("Launch board");
    vi.mocked(listGadgets).mockResolvedValue([DRAFT]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete gadget" }));

    await waitFor(() => expect(deleteGadget).toHaveBeenCalledWith("board"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.queryByRole("button", { name: "Open Launch board" })).toBeNull();
  });

  it("opens New gadget from New and from the empty state", async () => {
    vi.mocked(listGadgets).mockResolvedValue([]);
    vi.mocked(listSharedGadgets).mockResolvedValue([]);
    renderWithGadgets(<GadgetListPage />, { path: "/gadget" });

    expect(await screen.findByText("No gadgets yet")).toBeInTheDocument();
    expect(
      screen.getByText("Nothing yet. Gadgets people share with you or your groups show up here."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create gadget" }));
    const dialog = await screen.findByRole("dialog", { name: "New gadget" });
    expect(within(dialog).getByRole("textbox", { name: "Describe the gadget" })).toHaveFocus();
    expect(
      within(dialog).getByRole("heading", { name: "Or start from a template" }),
    ).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByTestId("new-gadget-button"));
    expect(await screen.findByRole("dialog", { name: "New gadget" })).toBeInTheDocument();
  });

  it("explains a failed load and retries", async () => {
    vi.mocked(listGadgets).mockRejectedValueOnce(new Error("The server is unavailable."));
    renderWithGadgets(<GadgetListPage />, { path: "/gadget" });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn’t load your gadgets");
    expect(alert).toHaveTextContent("The server is unavailable.");
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: "Open Launch board" })).toBeInTheDocument();
  });

  it("pins a gadget from its row", async () => {
    vi.mocked(pinGadget).mockResolvedValue({
      pinned: [{ id: "board", name: "Launch board" }],
      hasOwned: true,
    });
    renderWithGadgets(<GadgetListPage />, { path: "/gadget" });

    const menu = await screen.findByRole("button", { name: "Actions for Launch board" });
    await waitFor(() => expect(getGadgetShelf).toHaveBeenCalled());
    fireEvent.pointerDown(menu, { button: 0 });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Pin to sidebar" }));
    await waitFor(() => expect(pinGadget).toHaveBeenCalledWith("board", true));
    expect(await screen.findByText("Published · Pinned to sidebar")).toBeInTheDocument();
  });

  it("says so when pinning a gadget from its row fails", async () => {
    vi.mocked(pinGadget).mockRejectedValue(new Error("Synthetic outage"));
    renderWithGadgets(<GadgetListPage />, { path: "/gadget" });

    const menu = await screen.findByRole("button", { name: "Actions for Launch board" });
    await waitFor(() => expect(getGadgetShelf).toHaveBeenCalled());
    fireEvent.pointerDown(menu, { button: 0 });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Pin to sidebar" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Couldn’t pin the gadget"));
  });

  it("leaves out Shared with you where nothing can be shared", async () => {
    renderWithGadgets(<GadgetListPage />, {
      path: "/gadget",
      info: { ...GADGETS_INFO, single_user: true },
    });
    await screen.findByTestId("gadgets-list");
    expect(screen.queryByRole("heading", { name: "Shared with you" })).toBeNull();
    expect(listSharedGadgets).not.toHaveBeenCalled();
  });

  it("gives focus back to New when New gadget closes", async () => {
    renderWithGadgets(<GadgetListPage />, { path: "/gadget" });
    const newButton = await screen.findByTestId("new-gadget-button");
    fireEvent.click(newButton);
    const dialog = await screen.findByRole("dialog", { name: "New gadget" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(newButton).toHaveFocus();
  });
});
