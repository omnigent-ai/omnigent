import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  deleteThingy,
  getThingyShelf,
  listSharedThingies,
  listThingies,
  pinThingy,
} from "@/thingies/api/thingyApi";
import { ThingyListPage } from "./ThingyListPage";
import { THINGIES_INFO, renderWithThingies, thingyFixture } from "./testUtils";
import type * as ThingyApiModule from "@/thingies/api/thingyApi";

vi.mock("@/thingies/api/thingyApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ThingyApiModule>()),
  listThingies: vi.fn(),
  listSharedThingies: vi.fn(),
  deleteThingy: vi.fn(),
  getThingyShelf: vi.fn(),
  pinThingy: vi.fn(),
}));
// The template thumbnails run real thingies; their frame has its own tests.
vi.mock("@/thingies/host/ThingyFrame", () => ({ ThingyFrame: () => null }));

const BOARD = thingyFixture({ id: "board", name: "Launch board" });
const DRAFT = thingyFixture({
  id: "draft",
  name: "Reading list",
  published_revision: null,
  permissions: { read: true, write_data: true, publish: true, manage: false },
});

beforeEach(() => {
  vi.mocked(getThingyShelf).mockResolvedValue({ pinned: [], hasOwned: true });
  vi.mocked(listThingies).mockResolvedValue([BOARD, DRAFT]);
  vi.mocked(listSharedThingies).mockResolvedValue([
    {
      ...thingyFixture({ id: "team", name: "Team tracker" }),
      shared_via: "group",
      owner_email: "lee@example.com",
    },
    { ...thingyFixture({ id: "kpi", name: "KPI story" }), shared_via: "user", owner_email: null },
  ]);
});

afterEach(() => vi.clearAllMocks());

describe("Thingy page", () => {
  it("lists the viewer's thingies and those shared with them, and opens one", async () => {
    renderWithThingies(<ThingyListPage />, { path: "/thingy" });

    const yours = await screen.findByTestId("thingies-list");
    expect(within(yours).getByRole("button", { name: "Open Launch board" })).toBeInTheDocument();
    expect(within(yours).getByText("Not published yet")).toBeInTheDocument();

    const shared = await screen.findByTestId("shared-thingies-list");
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
    // Only a thingy's managers delete it, and nobody changes the shared list's.
    expect(screen.queryByRole("button", { name: "Actions for Team tracker" })).toBeNull();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Actions for Reading list" }), {
      button: 0,
    });
    expect(await screen.findByRole("menuitem", { name: "Rename" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Delete thingy" })).toBeNull();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });

    fireEvent.click(within(yours).getByRole("button", { name: "Open Launch board" }));
    expect(screen.getByLabelText("location")).toHaveTextContent("/thingy/board");
    // Opened from inside Omnigent, whatever key the host's router gives the entry.
    expect(screen.getByLabelText("location")).toHaveAttribute(
      "data-state",
      JSON.stringify({ keepSidebar: true }),
    );
  });

  it("deletes a thingy after confirming", async () => {
    vi.mocked(deleteThingy).mockResolvedValue(undefined);
    renderWithThingies(<ThingyListPage />, { path: "/thingy" });

    const menu = await screen.findByRole("button", { name: "Actions for Launch board" });
    fireEvent.pointerDown(menu, { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete thingy" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete thingy?" });
    expect(dialog).toHaveTextContent("Launch board");
    vi.mocked(listThingies).mockResolvedValue([DRAFT]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete thingy" }));

    await waitFor(() => expect(deleteThingy).toHaveBeenCalledWith("board"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.queryByRole("button", { name: "Open Launch board" })).toBeNull();
  });

  it("opens New thingy from New and from the empty state", async () => {
    vi.mocked(listThingies).mockResolvedValue([]);
    vi.mocked(listSharedThingies).mockResolvedValue([]);
    renderWithThingies(<ThingyListPage />, { path: "/thingy" });

    expect(await screen.findByText("No thingies yet")).toBeInTheDocument();
    expect(
      screen.getByText("Nothing yet. Thingies people share with you or your groups show up here."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create thingy" }));
    const dialog = await screen.findByRole("dialog", { name: "New thingy" });
    expect(within(dialog).getByRole("textbox", { name: "Describe the thingy" })).toHaveFocus();
    expect(
      within(dialog).getByRole("heading", { name: "Or start from a template" }),
    ).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(screen.getByTestId("new-thingy-button"));
    expect(await screen.findByRole("dialog", { name: "New thingy" })).toBeInTheDocument();
  });

  it("explains a failed load and retries", async () => {
    vi.mocked(listThingies).mockRejectedValueOnce(new Error("The server is unavailable."));
    renderWithThingies(<ThingyListPage />, { path: "/thingy" });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn’t load your thingies");
    expect(alert).toHaveTextContent("The server is unavailable.");
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: "Open Launch board" })).toBeInTheDocument();
  });

  it("pins a thingy from its row", async () => {
    vi.mocked(pinThingy).mockResolvedValue({
      pinned: [{ id: "board", name: "Launch board" }],
      hasOwned: true,
    });
    renderWithThingies(<ThingyListPage />, { path: "/thingy" });

    const menu = await screen.findByRole("button", { name: "Actions for Launch board" });
    await waitFor(() => expect(getThingyShelf).toHaveBeenCalled());
    fireEvent.pointerDown(menu, { button: 0 });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Pin to sidebar" }));
    await waitFor(() => expect(pinThingy).toHaveBeenCalledWith("board", true));
    expect(await screen.findByText("Published · Pinned to sidebar")).toBeInTheDocument();
  });

  it("leaves out Shared with you where nothing can be shared", async () => {
    renderWithThingies(<ThingyListPage />, {
      path: "/thingy",
      info: { ...THINGIES_INFO, single_user: true },
    });
    await screen.findByTestId("thingies-list");
    expect(screen.queryByRole("heading", { name: "Shared with you" })).toBeNull();
    expect(listSharedThingies).not.toHaveBeenCalled();
  });

  it("gives focus back to New when New thingy closes", async () => {
    renderWithThingies(<ThingyListPage />, { path: "/thingy" });
    const newButton = await screen.findByTestId("new-thingy-button");
    fireEvent.click(newButton);
    const dialog = await screen.findByRole("dialog", { name: "New thingy" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(newButton).toHaveFocus();
  });
});
