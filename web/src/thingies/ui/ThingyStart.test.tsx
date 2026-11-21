import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createThingy,
  createThingyRevision,
  publishThingyRevision,
} from "@/thingies/api/thingyApi";
import { THINGY_START_PARAM } from "@/thingies/host/thingyPages";
import { THINGY_INTRO, ThingyStartDialog, ThingyStartPage } from "./ThingyStart";
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

  it("says what a thingy is first", () => {
    renderWithThingies(<ThingyStartDialog open onOpenChange={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "New thingy" });
    expect(within(dialog).getByText(THINGY_INTRO)).toBeInTheDocument();
    expect(within(dialog).getByText(/Say what it shows/)).toBeInTheDocument();
  });

  it("previews a template, then creates, publishes and opens it in one step", async () => {
    const created = thingyFixture({ id: "where-1", name: "Where was I?", version: "v0" });
    vi.mocked(createThingy).mockResolvedValue(created);
    vi.mocked(createThingyRevision).mockResolvedValue({ id: "rev-9" });
    vi.mocked(publishThingyRevision).mockResolvedValue(created);
    renderWithThingies(<ThingyStartPage />, { path: "/thingy/new" });

    fireEvent.click(screen.getByRole("button", { name: "Where was I?" }));
    const preview = await screen.findByRole("dialog", { name: "Where was I?" });
    expect(within(preview).getByTestId("thingy-frame")).toBeInTheDocument();
    fireEvent.click(within(preview).getByRole("button", { name: "Use template" }));

    await waitFor(() => expect(location().pathname).toBe("/thingy/where-1"));
    expect(screen.queryByRole("dialog", { name: "Name your thingy" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("location")).toHaveAttribute(
      "data-state",
      JSON.stringify({ keepSidebar: true }),
    );
    expect(createThingy).toHaveBeenCalledWith("Where was I?");
    expect(createThingyRevision).toHaveBeenCalledWith(
      "where-1",
      expect.objectContaining({ html: expect.any(String) }),
    );
    expect(publishThingyRevision).toHaveBeenCalledWith("where-1", "rev-9", "v0");
  });

  it("tries a failed template create again from the step that failed", async () => {
    const created = thingyFixture({ id: "where-2", name: "Where was I?", version: "v0" });
    vi.mocked(createThingy).mockResolvedValue(created);
    vi.mocked(createThingyRevision)
      .mockRejectedValueOnce(new Error("Upload failed."))
      .mockResolvedValue({ id: "rev-2" });
    vi.mocked(publishThingyRevision).mockResolvedValue(created);
    renderWithThingies(<ThingyStartPage />, { path: "/thingy/new" });

    fireEvent.click(screen.getByRole("button", { name: "Where was I?" }));
    const preview = await screen.findByRole("dialog", { name: "Where was I?" });
    fireEvent.click(within(preview).getByRole("button", { name: "Use template" }));

    const alert = await within(preview).findByRole("alert");
    expect(alert).toHaveTextContent("Couldn’t create the thingy");
    expect(alert).toHaveTextContent("Upload failed.");
    fireEvent.click(within(preview).getByRole("button", { name: "Try again" }));

    await waitFor(() => expect(location().pathname).toBe("/thingy/where-2"));
    // The thingy from the first try is reused, not made twice.
    expect(createThingy).toHaveBeenCalledTimes(1);
    expect(publishThingyRevision).toHaveBeenCalledWith("where-2", "rev-2", "v0");
  });

  it("starts a blank thingy in New session with one click", async () => {
    vi.mocked(createThingy).mockResolvedValue(CREATED);
    renderWithThingies(<ThingyStartPage />, { path: "/thingy/new" });

    const blank = screen.getByRole("button", { name: "Blank" });
    fireEvent.click(blank);
    // A second click while it is being made does not make another.
    fireEvent.click(blank);

    await waitFor(() => expect(location().searchParams.get(THINGY_START_PARAM)).toBe("new-1"));
    expect(createThingy).toHaveBeenCalledTimes(1);
    expect(createThingy).toHaveBeenCalledWith("Untitled thingy");
    expect(createThingyRevision).not.toHaveBeenCalled();
    // The build asks the session to name it.
    expect(location().searchParams.get("prompt")).toMatch(/give it a short name/);
  });

  it("shows a failed blank create under the templates, with Retry", async () => {
    vi.mocked(createThingy)
      .mockRejectedValueOnce(new Error("Thingies are turned off."))
      .mockResolvedValue(CREATED);
    renderWithThingies(<ThingyStartPage />, { path: "/thingy/new" });

    fireEvent.click(screen.getByRole("button", { name: "Blank" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Thingies are turned off.");
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(location().searchParams.get(THINGY_START_PARAM)).toBe("new-1"));
  });
});
