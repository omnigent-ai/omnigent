import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createThingy,
  createThingyRevision,
  publishThingyRevision,
} from "@/thingies/api/thingyApi";
import { THINGY_START_PARAM } from "@/thingies/host/thingyPages";
import { ThingyStartDialog, ThingyStartPage } from "./ThingyStart";
import { renderWithThingies, thingyFixture } from "./testUtils";
import type * as ThingyApiModule from "@/thingies/api/thingyApi";

vi.mock("@/thingies/api/thingyApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ThingyApiModule>()),
  createThingy: vi.fn(),
  createThingyRevision: vi.fn(),
  publishThingyRevision: vi.fn(),
}));
vi.mock("@/thingies/host/ThingyFrame", () => ({
  ThingyFrame: () => <div data-testid="thingy-frame" />,
}));

const CREATED = thingyFixture({ id: "new-1", name: "Incident tracker", published_revision: null });

function location() {
  return new URL(screen.getByLabelText("location").textContent ?? "", "https://example.com");
}

afterEach(() => vi.clearAllMocks());

describe("New thingy", () => {
  it("builds the description into New session, with the thingy beside it", async () => {
    vi.mocked(createThingy).mockResolvedValue(CREATED);
    const onOpenChange = vi.fn();
    renderWithThingies(<ThingyStartDialog open onOpenChange={onOpenChange} />, {
      path: "/thingy",
    });

    const dialog = screen.getByRole("dialog", { name: "New thingy" });
    const build = within(dialog).getByRole("button", { name: "Build" });
    expect(build).toBeDisabled();
    fireEvent.click(
      within(dialog).getByRole("button", { name: "An incident tracker for my team" }),
    );
    const description = within(dialog).getByRole("textbox", { name: "Describe the thingy" });
    expect(description).toHaveValue("An incident tracker for my team");
    fireEvent.keyDown(description, { key: "Enter", ctrlKey: true });

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(createThingy).toHaveBeenCalledWith("An incident tracker for my team");
    const opened = location();
    expect(opened.pathname).toBe("/");
    expect(opened.searchParams.get(THINGY_START_PARAM)).toBe("new-1");
    // The session is not started here: the build instructions wait in the composer.
    expect(opened.searchParams.get("prompt")).toMatch(/thingy_id new-1/);
    expect(opened.searchParams.get("prompt")).toMatch(/An incident tracker for my team$/);
  });

  it("keeps the description and explains a failed build", async () => {
    vi.mocked(createThingy).mockRejectedValue(new Error("Thingies are turned off."));
    renderWithThingies(<ThingyStartDialog open onOpenChange={vi.fn()} />);

    const dialog = screen.getByRole("dialog", { name: "New thingy" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Describe the thingy" }), {
      target: { value: "A reading list" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Build" }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Couldn’t create the thingy");
    expect(alert).toHaveTextContent("Thingies are turned off.");
    expect(within(dialog).getByRole("textbox", { name: "Describe the thingy" })).toHaveValue(
      "A reading list",
    );
  });

  it("previews a template, names it, publishes it and opens it", async () => {
    const created = thingyFixture({ id: "where-1", name: "Team where was I", version: "v0" });
    vi.mocked(createThingy).mockResolvedValue(created);
    vi.mocked(createThingyRevision).mockResolvedValue({ id: "rev-9" });
    vi.mocked(publishThingyRevision).mockResolvedValue(created);
    renderWithThingies(<ThingyStartPage />, { path: "/thingy/new" });

    fireEvent.click(screen.getByRole("button", { name: "Where was I?" }));
    const preview = await screen.findByRole("dialog", { name: "Where was I?" });
    expect(within(preview).getByTestId("thingy-frame")).toBeInTheDocument();
    fireEvent.click(within(preview).getByRole("button", { name: "Use template" }));

    const naming = await screen.findByRole("dialog", { name: "Name your thingy" });
    const name = within(naming).getByRole("textbox", { name: "Name" });
    expect(name).toHaveValue("Where was I?");
    fireEvent.change(name, { target: { value: "Team where was I" } });
    fireEvent.click(within(naming).getByRole("button", { name: "Create" }));

    await waitFor(() => expect(location().pathname).toBe("/thingy/where-1"));
    expect(screen.getByLabelText("location")).toHaveAttribute(
      "data-state",
      JSON.stringify({ keepSidebar: true }),
    );
    expect(createThingy).toHaveBeenCalledWith("Team where was I");
    expect(createThingyRevision).toHaveBeenCalledWith(
      "where-1",
      expect.objectContaining({ html: expect.any(String) }),
    );
    expect(publishThingyRevision).toHaveBeenCalledWith("where-1", "rev-9", "v0");
  });

  it("starts a blank thingy in New session", async () => {
    vi.mocked(createThingy).mockResolvedValue(CREATED);
    renderWithThingies(<ThingyStartPage />, { path: "/thingy/new" });

    fireEvent.click(screen.getByRole("button", { name: "Blank" }));
    const naming = await screen.findByRole("dialog", { name: "Name your thingy" });
    fireEvent.click(within(naming).getByRole("button", { name: "Create" }));

    await waitFor(() => expect(location().searchParams.get(THINGY_START_PARAM)).toBe("new-1"));
    expect(createThingyRevision).not.toHaveBeenCalled();
  });

  it("refuses a name that is too long", async () => {
    renderWithThingies(<ThingyStartPage />, { path: "/thingy/new" });
    fireEvent.click(screen.getByRole("button", { name: "Blank" }));
    const naming = await screen.findByRole("dialog", { name: "Name your thingy" });
    fireEvent.change(within(naming).getByRole("textbox", { name: "Name" }), {
      target: { value: "x".repeat(121) },
    });
    // The server's limit; typing stops there, so only a pasted name can pass it.
    expect(within(naming).getByText("Use at most 120 characters.")).toBeInTheDocument();
    expect(within(naming).getByRole("button", { name: "Create" })).toBeDisabled();
  });
});
