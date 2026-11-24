import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type Gadget,
  createGadget,
  createGadgetRevision,
  deleteGadget,
  publishGadgetRevision,
} from "@/gadgets/api/gadgetApi";
import { GADGET_START_PARAM } from "@/gadgets/host/gadgetPages";
import { GADGET_INTRO, GadgetStartDialog, GadgetStartPage } from "./GadgetStart";
import { renderWithGadgets, gadgetFixture } from "./testUtils";
import type * as GadgetApiModule from "@/gadgets/api/gadgetApi";

vi.mock("@/gadgets/api/gadgetApi", async (importOriginal) => ({
  ...(await importOriginal<typeof GadgetApiModule>()),
  createGadget: vi.fn(),
  deleteGadget: vi.fn(),
  createGadgetRevision: vi.fn(),
  publishGadgetRevision: vi.fn(),
}));
vi.mock("@/gadgets/host/GadgetFrame", () => ({
  GadgetFrame: () => <div data-testid="gadget-frame" />,
}));

const CREATED = gadgetFixture({ id: "new-1", name: "Incident tracker", published_revision: null });

function location() {
  return new URL(screen.getByLabelText("location").textContent ?? "", "https://example.com");
}

afterEach(() => vi.clearAllMocks());

describe("New gadget", () => {
  it("builds the description into New session, with the gadget beside it", async () => {
    vi.mocked(createGadget).mockResolvedValue(CREATED);
    const onOpenChange = vi.fn();
    renderWithGadgets(<GadgetStartDialog open onOpenChange={onOpenChange} />, {
      path: "/gadget",
    });

    const dialog = screen.getByRole("dialog", { name: "New gadget" });
    const build = within(dialog).getByRole("button", { name: "Build" });
    expect(build).toBeDisabled();
    fireEvent.click(
      within(dialog).getByRole("button", { name: "An incident tracker for my team" }),
    );
    const description = within(dialog).getByRole("textbox", { name: "Describe the gadget" });
    expect(description).toHaveValue("An incident tracker for my team");
    fireEvent.keyDown(description, { key: "Enter", ctrlKey: true });

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(createGadget).toHaveBeenCalledWith("An incident tracker for my team");
    const opened = location();
    expect(opened.pathname).toBe("/");
    expect(opened.searchParams.get(GADGET_START_PARAM)).toBe("new-1");
    // The session is not started here: the build instructions wait in the composer.
    expect(opened.searchParams.get("prompt")).toMatch(/gadget_id new-1/);
    expect(opened.searchParams.get("prompt")).toMatch(/An incident tracker for my team$/);
  });

  it("deletes instead of opening a gadget whose create finishes after Cancel", async () => {
    let finishCreate: (gadget: Gadget) => void = () => {};
    vi.mocked(createGadget).mockReturnValue(
      new Promise<Gadget>((resolve) => {
        finishCreate = resolve;
      }),
    );
    vi.mocked(deleteGadget).mockResolvedValue(undefined);
    const onOpenChange = vi.fn();
    renderWithGadgets(<GadgetStartDialog open onOpenChange={onOpenChange} />, {
      path: "/gadget",
    });
    const dialog = screen.getByRole("dialog", { name: "New gadget" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Describe the gadget" }), {
      target: { value: "A reading list" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Build" }));
    await waitFor(() => expect(createGadget).toHaveBeenCalled());
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    finishCreate(CREATED);
    await waitFor(() => expect(deleteGadget).toHaveBeenCalledWith("new-1"));
    expect(location().pathname).toBe("/gadget");
  });

  it("keeps the description and explains a failed build", async () => {
    vi.mocked(createGadget).mockRejectedValue(new Error("Gadgets are turned off."));
    renderWithGadgets(<GadgetStartDialog open onOpenChange={vi.fn()} />);

    const dialog = screen.getByRole("dialog", { name: "New gadget" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Describe the gadget" }), {
      target: { value: "A reading list" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Build" }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Couldn’t create the gadget");
    expect(alert).toHaveTextContent("Gadgets are turned off.");
    expect(within(dialog).getByRole("textbox", { name: "Describe the gadget" })).toHaveValue(
      "A reading list",
    );
  });

  it("says what a gadget is first", () => {
    renderWithGadgets(<GadgetStartDialog open onOpenChange={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "New gadget" });
    expect(within(dialog).getByText(GADGET_INTRO)).toBeInTheDocument();
    expect(within(dialog).getByText(/Say what it shows/)).toBeInTheDocument();
  });

  it("previews a template, then creates, publishes and opens it in one step", async () => {
    const created = gadgetFixture({ id: "where-1", name: "Where was I?", version: "v0" });
    vi.mocked(createGadget).mockResolvedValue(created);
    vi.mocked(createGadgetRevision).mockResolvedValue({ id: "rev-9" });
    vi.mocked(publishGadgetRevision).mockResolvedValue(created);
    renderWithGadgets(<GadgetStartPage />, { path: "/gadget/new" });

    fireEvent.click(screen.getByRole("button", { name: "Where was I?" }));
    const preview = await screen.findByRole("dialog", { name: "Where was I?" });
    expect(within(preview).getByTestId("gadget-frame")).toBeInTheDocument();
    fireEvent.click(within(preview).getByRole("button", { name: "Use template" }));

    await waitFor(() => expect(location().pathname).toBe("/gadget/where-1"));
    expect(screen.queryByRole("dialog", { name: "Name your gadget" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("location")).toHaveAttribute(
      "data-state",
      JSON.stringify({ keepSidebar: true }),
    );
    expect(createGadget).toHaveBeenCalledWith("Where was I?");
    expect(createGadgetRevision).toHaveBeenCalledWith(
      "where-1",
      expect.objectContaining({ html: expect.any(String) }),
    );
    expect(publishGadgetRevision).toHaveBeenCalledWith("where-1", "rev-9", "v0");
  });

  it("tries a failed template create again from the step that failed", async () => {
    const created = gadgetFixture({ id: "where-2", name: "Where was I?", version: "v0" });
    vi.mocked(createGadget).mockResolvedValue(created);
    vi.mocked(createGadgetRevision)
      .mockRejectedValueOnce(new Error("Upload failed."))
      .mockResolvedValue({ id: "rev-2" });
    vi.mocked(publishGadgetRevision).mockResolvedValue(created);
    renderWithGadgets(<GadgetStartPage />, { path: "/gadget/new" });

    fireEvent.click(screen.getByRole("button", { name: "Where was I?" }));
    const preview = await screen.findByRole("dialog", { name: "Where was I?" });
    fireEvent.click(within(preview).getByRole("button", { name: "Use template" }));

    const alert = await within(preview).findByRole("alert");
    expect(alert).toHaveTextContent("Couldn’t create the gadget");
    expect(alert).toHaveTextContent("Upload failed.");
    fireEvent.click(within(preview).getByRole("button", { name: "Try again" }));

    await waitFor(() => expect(location().pathname).toBe("/gadget/where-2"));
    // The gadget from the first try is reused, not made twice.
    expect(createGadget).toHaveBeenCalledTimes(1);
    expect(publishGadgetRevision).toHaveBeenCalledWith("where-2", "rev-2", "v0");
  });

  it("publishes the uploaded revision again when only the publish failed", async () => {
    const created = gadgetFixture({ id: "where-3", name: "Where was I?", version: "v0" });
    vi.mocked(createGadget).mockResolvedValue(created);
    vi.mocked(createGadgetRevision).mockResolvedValue({ id: "rev-3" });
    vi.mocked(publishGadgetRevision)
      .mockRejectedValueOnce(new Error("Publish failed."))
      .mockResolvedValue(created);
    renderWithGadgets(<GadgetStartPage />, { path: "/gadget/new" });

    fireEvent.click(screen.getByRole("button", { name: "Where was I?" }));
    const preview = await screen.findByRole("dialog", { name: "Where was I?" });
    fireEvent.click(within(preview).getByRole("button", { name: "Use template" }));
    expect(await within(preview).findByRole("alert")).toHaveTextContent("Publish failed.");
    fireEvent.click(within(preview).getByRole("button", { name: "Try again" }));

    await waitFor(() => expect(location().pathname).toBe("/gadget/where-3"));
    // Neither the gadget nor its revision is made twice.
    expect(createGadget).toHaveBeenCalledTimes(1);
    expect(createGadgetRevision).toHaveBeenCalledTimes(1);
    expect(publishGadgetRevision).toHaveBeenCalledTimes(2);
    expect(publishGadgetRevision).toHaveBeenLastCalledWith("where-3", "rev-3", "v0");
  });

  it("starts a blank gadget in New session with one click", async () => {
    vi.mocked(createGadget).mockResolvedValue(CREATED);
    renderWithGadgets(<GadgetStartPage />, { path: "/gadget/new" });

    const blank = screen.getByRole("button", { name: "Blank" });
    fireEvent.click(blank);
    // A second click while it is being made does not make another.
    fireEvent.click(blank);

    await waitFor(() => expect(location().searchParams.get(GADGET_START_PARAM)).toBe("new-1"));
    expect(createGadget).toHaveBeenCalledTimes(1);
    expect(createGadget).toHaveBeenCalledWith("Untitled gadget");
    expect(createGadgetRevision).not.toHaveBeenCalled();
    // The build asks the session to name it.
    expect(location().searchParams.get("prompt")).toMatch(/give it a short name/);
  });

  it("shows a failed blank create under the templates, with Retry", async () => {
    vi.mocked(createGadget)
      .mockRejectedValueOnce(new Error("Gadgets are turned off."))
      .mockResolvedValue(CREATED);
    renderWithGadgets(<GadgetStartPage />, { path: "/gadget/new" });

    fireEvent.click(screen.getByRole("button", { name: "Blank" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Gadgets are turned off.");
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(location().searchParams.get(GADGET_START_PARAM)).toBe("new-1"));
  });
});
