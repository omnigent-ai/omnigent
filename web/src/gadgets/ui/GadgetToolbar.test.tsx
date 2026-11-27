import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { toast } from "sonner";

import { getGadgetShelf, pinGadget, renameGadget } from "@/gadgets/api/gadgetApi";
import { copyText } from "@/lib/clipboard";
import { GadgetActions, GadgetName, GadgetPinButton } from "./GadgetToolbar";
import { GADGETS_INFO, renderWithGadgets, gadgetFixture } from "./testUtils";
import type * as GadgetApiModule from "@/gadgets/api/gadgetApi";

vi.mock("@/gadgets/api/gadgetApi", async (importOriginal) => ({
  ...(await importOriginal<typeof GadgetApiModule>()),
  getGadgetShelf: vi.fn(),
  pinGadget: vi.fn(),
  renameGadget: vi.fn(),
}));
// The share dialog has its own tests; here it only says it opened.
vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn(() => Promise.resolve()) }));

vi.mock("@/components/PermissionsModal", () => ({
  PermissionsModal: ({ open, gadget }: { open: boolean; gadget: { name: string } }) =>
    open ? <div role="dialog" aria-label={`Share ${gadget.name}`} /> : null,
}));

const BOARD = gadgetFixture({ id: "board", name: "Launch board" });
const VIEW_ONLY = gadgetFixture({
  id: "board",
  name: "Launch board",
  permissions: { read: true, write_data: false, publish: false, manage: false },
});

beforeEach(() => {
  vi.mocked(getGadgetShelf).mockResolvedValue({ pinned: [], hasOwned: true });
});
afterEach(() => vi.clearAllMocks());

describe("the gadget's name", () => {
  it("renames in place: Enter saves, Escape cancels", async () => {
    vi.mocked(renameGadget).mockResolvedValue({ ...BOARD, name: "Release board" });
    renderWithGadgets(<GadgetName gadget={BOARD} />);

    fireEvent.click(screen.getByRole("button", { name: "Rename Launch board" }));
    let field = screen.getByRole("textbox", { name: "Gadget name" });
    fireEvent.change(field, { target: { value: "Nope" } });
    fireEvent.keyDown(field, { key: "Escape" });
    expect(screen.getByRole("button", { name: "Rename Launch board" })).toBeInTheDocument();
    expect(renameGadget).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Rename Launch board" }));
    field = screen.getByRole("textbox", { name: "Gadget name" });
    fireEvent.change(field, { target: { value: "  Release board  " } });
    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(renameGadget).toHaveBeenCalledWith("board", "Release board"));
    await waitFor(() => expect(screen.queryByRole("textbox")).toBeNull());
  });

  it("saves on leaving the field, and keeps an unchanged name without a request", async () => {
    vi.mocked(renameGadget).mockRejectedValue(new Error("Name taken."));
    renderWithGadgets(<GadgetName gadget={BOARD} />);
    fireEvent.click(screen.getByRole("button", { name: "Rename Launch board" }));
    fireEvent.blur(screen.getByRole("textbox", { name: "Gadget name" }));
    expect(renameGadget).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Rename Launch board" }));
    const field = screen.getByRole("textbox", { name: "Gadget name" });
    fireEvent.change(field, { target: { value: "Other" } });
    fireEvent.blur(field);
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t rename: Name taken.");
  });

  it("is plain text for viewers who can't edit", () => {
    renderWithGadgets(<GadgetName gadget={VIEW_ONLY} />);
    expect(screen.getByText("Launch board")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("Pin", () => {
  it("pins the gadget to the viewer's sidebar, then offers to unpin it", async () => {
    vi.mocked(pinGadget).mockResolvedValue({
      pinned: [{ id: "board", name: "Launch board" }],
      hasOwned: true,
    });
    renderWithGadgets(<GadgetPinButton gadget={BOARD} />);

    const pin = await screen.findByRole("button", { name: "Pin to sidebar" });
    await waitFor(() => expect(pin).toBeEnabled());
    fireEvent.click(pin);
    await waitFor(() => expect(pinGadget).toHaveBeenCalledWith("board", true));
    const unpin = await screen.findByRole("button", { name: "Unpin from sidebar" });
    expect(unpin).toHaveAttribute("aria-pressed", "true");
    // A filled pin, not a crossed-out one, shows it is pinned.
    expect(unpin.querySelector("svg")).toHaveClass("fill-current");
  });
});

describe("Share and the ⋯ menu", () => {
  function openMenu() {
    fireEvent.pointerDown(screen.getByRole("button", { name: "More gadget actions" }), {
      button: 0,
    });
  }

  it("offers Share, details and Delete to managers", async () => {
    renderWithGadgets(<GadgetActions gadget={BOARD} onDeleted={vi.fn()} shareInMenu={false} />);

    fireEvent.click(screen.getByRole("button", { name: "Share gadget" }));
    expect(screen.getByRole("dialog", { name: "Share Launch board" })).toBeInTheDocument();

    openMenu();
    expect(await screen.findByRole("menuitem", { name: "Delete gadget" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: "Gadget details" }));
    const details = await screen.findByRole("dialog", { name: "Gadget details" });
    // Names, not internal IDs: the viewer owns it, and its link is what people open.
    expect(within(details).queryByText("rev-1")).toBeNull();
    expect(within(details).getByText("Published")).toBeInTheDocument();
    expect(within(details).getByText("You")).toBeInTheDocument();
    expect(within(details).getByText(/\/gadget\/board$/)).toBeInTheDocument();
    expect(within(details).getByRole("button", { name: "Copy link" })).toBeInTheDocument();

    // A copy the browser refuses says so instead of failing quietly.
    vi.mocked(copyText).mockRejectedValueOnce(new Error("denied"));
    fireEvent.click(within(details).getByRole("button", { name: "Copy link" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("Couldn’t copy the link"));
    expect(within(details).getByRole("button", { name: "Copy link" })).toBeInTheDocument();
  });

  it("names someone else as the owner", async () => {
    renderWithGadgets(
      <GadgetActions
        gadget={{ ...VIEW_ONLY, viewer_id: "bob@example.com" }}
        onDeleted={vi.fn()}
        shareInMenu={false}
      />,
    );
    openMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Gadget details" }));
    const details = await screen.findByRole("dialog", { name: "Gadget details" });
    expect(within(details).getByText("owner@example.com")).toBeInTheDocument();
  });

  it("moves Share into the menu where the bar has little room", async () => {
    renderWithGadgets(<GadgetActions gadget={BOARD} onDeleted={vi.fn()} shareInMenu />);
    expect(screen.queryByRole("button", { name: "Share gadget" })).toBeNull();
    openMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Share" }));
    expect(screen.getByRole("dialog", { name: "Share Launch board" })).toBeInTheDocument();
  });

  it("offers only the details to viewers, and no Share on a single-user server", async () => {
    const { unmount } = renderWithGadgets(
      <GadgetActions gadget={VIEW_ONLY} onDeleted={vi.fn()} shareInMenu={false} />,
    );
    expect(screen.queryByRole("button", { name: "Share gadget" })).toBeNull();
    openMenu();
    expect(await screen.findByRole("menuitem", { name: "Gadget details" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Delete gadget" })).toBeNull();
    unmount();

    renderWithGadgets(<GadgetActions gadget={BOARD} onDeleted={vi.fn()} shareInMenu={false} />, {
      info: { ...GADGETS_INFO, single_user: true },
    });
    expect(screen.queryByRole("button", { name: "Share gadget" })).toBeNull();
  });

  it("offers no Share on a multi-user server whose store keeps no gadget grants", async () => {
    renderWithGadgets(<GadgetActions gadget={BOARD} onDeleted={vi.fn()} shareInMenu={false} />, {
      info: { ...GADGETS_INFO, gadget_sharing: false },
    });
    expect(screen.queryByRole("button", { name: "Share gadget" })).toBeNull();
    openMenu();
    expect(await screen.findByRole("menuitem", { name: "Delete gadget" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Share" })).toBeNull();
  });
});
