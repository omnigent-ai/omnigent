import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { deleteThingy, listSharedThingies, listThingies } from "@/thingies/api/thingyApi";
import { ThingyListPage } from "./ThingyListPage";
import { renderWithThingies, thingyFixture } from "./testUtils";
import type * as ThingyApiModule from "@/thingies/api/thingyApi";

vi.mock("@/thingies/api/thingyApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ThingyApiModule>()),
  listThingies: vi.fn(),
  listSharedThingies: vi.fn(),
  deleteThingy: vi.fn(),
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
    // Only a thingy's managers delete it, and nobody deletes from the shared list.
    expect(screen.getByRole("button", { name: "Actions for Launch board" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Actions for Reading list" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Actions for Team tracker" })).toBeNull();

    fireEvent.click(within(yours).getByRole("button", { name: "Open Launch board" }));
    expect(screen.getByLabelText("location")).toHaveTextContent("/thingy/board");
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
});
