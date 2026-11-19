import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getThingyShelf, pinThingy, renameThingy } from "@/thingies/api/thingyApi";
import { ThingyActions, ThingyName, ThingyPinButton } from "./ThingyToolbar";
import { THINGIES_INFO, renderWithThingies, thingyFixture } from "./testUtils";
import type * as ThingyApiModule from "@/thingies/api/thingyApi";

vi.mock("@/thingies/api/thingyApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ThingyApiModule>()),
  getThingyShelf: vi.fn(),
  pinThingy: vi.fn(),
  renameThingy: vi.fn(),
}));
// The share dialog has its own tests; here it only says it opened.
vi.mock("@/components/PermissionsModal", () => ({
  PermissionsModal: ({ open, thingy }: { open: boolean; thingy: { name: string } }) =>
    open ? <div role="dialog" aria-label={`Share ${thingy.name}`} /> : null,
}));

const BOARD = thingyFixture({ id: "board", name: "Launch board" });
const VIEW_ONLY = thingyFixture({
  id: "board",
  name: "Launch board",
  permissions: { read: true, write_data: false, publish: false, manage: false },
});

beforeEach(() => {
  vi.mocked(getThingyShelf).mockResolvedValue({ pinned: [], hasOwned: true });
});
afterEach(() => vi.clearAllMocks());

describe("the thingy's name", () => {
  it("renames in place: Enter saves, Escape cancels", async () => {
    vi.mocked(renameThingy).mockResolvedValue({ ...BOARD, name: "Release board" });
    renderWithThingies(<ThingyName thingy={BOARD} />);

    fireEvent.click(screen.getByRole("button", { name: "Launch board" }));
    let field = screen.getByRole("textbox", { name: "Thingy name" });
    fireEvent.change(field, { target: { value: "Nope" } });
    fireEvent.keyDown(field, { key: "Escape" });
    expect(screen.getByRole("button", { name: "Launch board" })).toBeInTheDocument();
    expect(renameThingy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Launch board" }));
    field = screen.getByRole("textbox", { name: "Thingy name" });
    fireEvent.change(field, { target: { value: "  Release board  " } });
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(renameThingy).toHaveBeenCalledWith("board", "Release board"));
    await waitFor(() => expect(screen.queryByRole("textbox")).toBeNull());
  });

  it("saves on leaving the field, and keeps an unchanged name without a request", async () => {
    vi.mocked(renameThingy).mockRejectedValue(new Error("Name taken."));
    renderWithThingies(<ThingyName thingy={BOARD} />);
    fireEvent.click(screen.getByRole("button", { name: "Launch board" }));
    fireEvent.blur(screen.getByRole("textbox", { name: "Thingy name" }));
    expect(renameThingy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Launch board" }));
    const field = screen.getByRole("textbox", { name: "Thingy name" });
    fireEvent.change(field, { target: { value: "Other" } });
    fireEvent.blur(field);
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t rename: Name taken.");
  });

  it("is plain text for viewers who can't edit", () => {
    renderWithThingies(<ThingyName thingy={VIEW_ONLY} />);
    expect(screen.getByText("Launch board")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("Pin", () => {
  it("pins the thingy to the viewer's sidebar, then offers to unpin it", async () => {
    vi.mocked(pinThingy).mockResolvedValue({
      pinned: [{ id: "board", name: "Launch board" }],
      hasOwned: true,
    });
    renderWithThingies(<ThingyPinButton thingy={BOARD} />);

    const pin = await screen.findByRole("button", { name: "Pin to sidebar" });
    await waitFor(() => expect(pin).toBeEnabled());
    fireEvent.click(pin);
    await waitFor(() => expect(pinThingy).toHaveBeenCalledWith("board", true));
    expect(await screen.findByRole("button", { name: "Unpin from sidebar" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});

describe("Share and the ⋯ menu", () => {
  function openMenu() {
    fireEvent.pointerDown(screen.getByRole("button", { name: "More thingy actions" }), {
      button: 0,
    });
  }

  it("offers Share, details and Delete to managers", async () => {
    renderWithThingies(<ThingyActions thingy={BOARD} onDeleted={vi.fn()} shareInMenu={false} />);

    fireEvent.click(screen.getByRole("button", { name: "Share thingy" }));
    expect(screen.getByRole("dialog", { name: "Share Launch board" })).toBeInTheDocument();

    openMenu();
    expect(await screen.findByRole("menuitem", { name: "Delete thingy" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: "Thingy details" }));
    const details = await screen.findByRole("dialog", { name: "Thingy details" });
    expect(within(details).getByText("rev-1")).toBeInTheDocument();
    expect(within(details).getByText("owner@example.com")).toBeInTheDocument();
  });

  it("moves Share into the menu where the bar has little room", async () => {
    renderWithThingies(<ThingyActions thingy={BOARD} onDeleted={vi.fn()} shareInMenu />);
    expect(screen.queryByRole("button", { name: "Share thingy" })).toBeNull();
    openMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Share" }));
    expect(screen.getByRole("dialog", { name: "Share Launch board" })).toBeInTheDocument();
  });

  it("offers only the details to viewers, and no Share on a single-user server", async () => {
    const { unmount } = renderWithThingies(
      <ThingyActions thingy={VIEW_ONLY} onDeleted={vi.fn()} shareInMenu={false} />,
    );
    expect(screen.queryByRole("button", { name: "Share thingy" })).toBeNull();
    openMenu();
    expect(await screen.findByRole("menuitem", { name: "Thingy details" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Delete thingy" })).toBeNull();
    unmount();

    renderWithThingies(<ThingyActions thingy={BOARD} onDeleted={vi.fn()} shareInMenu={false} />, {
      info: { ...THINGIES_INFO, single_user: true },
    });
    expect(screen.queryByRole("button", { name: "Share thingy" })).toBeNull();
  });
});
