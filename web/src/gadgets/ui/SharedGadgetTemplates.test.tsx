import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createGadgetFromTemplate, listGadgetTemplateShares } from "@/gadgets/api/gadgetApi";
import type * as GadgetApiModule from "@/gadgets/api/gadgetApi";
import { SharedGadgetTemplates } from "./SharedGadgetTemplates";
import { GADGETS_INFO, gadgetFixture, renderWithGadgets } from "./testUtils";

vi.mock("@/gadgets/api/gadgetApi", async (importOriginal) => ({
  ...(await importOriginal<typeof GadgetApiModule>()),
  createGadgetFromTemplate: vi.fn(),
  listGadgetTemplateShares: vi.fn(),
}));

afterEach(() => vi.clearAllMocks());

const COURSE = { id: "course", name: "Onboarding course", owner_email: "alice@example.com" };

describe("templates shared with the viewer", () => {
  it("makes the viewer's own gadget from one", async () => {
    vi.mocked(listGadgetTemplateShares).mockResolvedValue([COURSE]);
    const made = gadgetFixture({ id: "mine", name: "Onboarding course" });
    vi.mocked(createGadgetFromTemplate).mockResolvedValue(made);
    const onCreated = vi.fn();
    renderWithGadgets(<SharedGadgetTemplates onCreated={onCreated} />);

    expect(await screen.findByText("From alice@example.com")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Onboarding course" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(made));
    expect(createGadgetFromTemplate).toHaveBeenCalledWith("course");
  });

  it("shows the failure with a way to try again", async () => {
    vi.mocked(listGadgetTemplateShares).mockResolvedValue([COURSE]);
    vi.mocked(createGadgetFromTemplate).mockRejectedValue(new Error("Template unpublished."));
    renderWithGadgets(<SharedGadgetTemplates onCreated={vi.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "Onboarding course" }));
    expect(await screen.findByText(/Template unpublished/)).toBeInTheDocument();
  });

  it("says when the templates couldn't be loaded, and loads them on retry", async () => {
    vi.mocked(listGadgetTemplateShares)
      .mockRejectedValueOnce(new Error("Server unavailable."))
      .mockResolvedValueOnce([COURSE]);
    renderWithGadgets(<SharedGadgetTemplates onCreated={vi.fn()} />);

    expect(await screen.findByText(/Server unavailable/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /try again|retry/i }));
    expect(await screen.findByRole("button", { name: "Onboarding course" })).toBeInTheDocument();
  });

  it("isn't shown where the server doesn't share gadgets, or nothing is shared", async () => {
    vi.mocked(listGadgetTemplateShares).mockResolvedValue([COURSE]);
    for (const info of [
      { ...GADGETS_INFO, gadget_sharing: false },
      // A server that shares gadgets but predates templates.
      { ...GADGETS_INFO, gadget_templates: undefined },
    ]) {
      const { unmount } = renderWithGadgets(<SharedGadgetTemplates onCreated={vi.fn()} />, {
        info,
      });
      expect(screen.queryByText("Shared with you")).toBeNull();
      unmount();
    }
    expect(listGadgetTemplateShares).not.toHaveBeenCalled();

    vi.mocked(listGadgetTemplateShares).mockResolvedValue([]);
    renderWithGadgets(<SharedGadgetTemplates onCreated={vi.fn()} />);
    await waitFor(() => expect(listGadgetTemplateShares).toHaveBeenCalled());
    expect(screen.queryByText("Shared with you")).toBeNull();
  });
});
