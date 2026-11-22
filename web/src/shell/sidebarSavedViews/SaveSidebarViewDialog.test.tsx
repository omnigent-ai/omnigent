import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SaveViewResult } from "@/lib/sidebarSavedViews";

import { SaveSidebarViewDialog } from "./SaveSidebarViewDialog";

function renderDialog(result: SaveViewResult) {
  const onSave = vi.fn(() => result);
  const onOpenChange = vi.fn();
  render(<SaveSidebarViewDialog open onOpenChange={onOpenChange} onSave={onSave} />);
  return { onSave, onOpenChange };
}

const nameField = () => screen.getByTestId("save-sidebar-view-dialog-name");
const saveButton = () => screen.getByTestId("save-sidebar-view-dialog-save");

describe("SaveSidebarViewDialog", () => {
  it("saves a name and closes", () => {
    const { onSave, onOpenChange } = renderDialog({
      ok: true,
      view: {
        id: "v1",
        name: "Review",
        grouping: "default",
        ordering: "updated",
        show: [],
        sessionFilter: "all",
        pullRequestFilter: "all",
        statusFilter: "all",
        harnessFilter: null,
        projectFilter: { kind: "all" },
      },
    });
    expect(saveButton()).toBeDisabled();
    fireEvent.change(nameField(), { target: { value: "Review" } });
    fireEvent.click(saveButton());
    expect(onSave).toHaveBeenCalledWith("Review");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it.each([
    ["duplicate", "A view with this name already exists."],
    ["limit", "You can keep up to 20 views. Delete one to save another."],
    ["storage", "Couldn't save the view on this device. Try again."],
  ] as const)(
    "stays open and says why on a %s refusal, until the name changes",
    (problem, text) => {
      const { onOpenChange } = renderDialog({ ok: false, problem });
      fireEvent.change(nameField(), { target: { value: "Review" } });
      fireEvent.click(saveButton());
      expect(screen.getByRole("alert")).toHaveTextContent(text);
      expect(nameField()).toHaveAttribute("aria-invalid", "true");
      expect(onOpenChange).not.toHaveBeenCalled();
      fireEvent.change(nameField(), { target: { value: "Review 2" } });
      expect(screen.queryByRole("alert")).toBeNull();
    },
  );

  it("caps the name at 40 characters", () => {
    renderDialog({ ok: false, problem: "empty" });
    expect(nameField()).toHaveAttribute("maxlength", "40");
  });
});
