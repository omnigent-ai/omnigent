import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { FilesPanelDrawer } from "./FilesPanelDrawer";

vi.mock("@/hooks/useResizablePanel", () => ({
  useResizablePanel: () => ({
    panelWidth: 400,
    handleProps: {
      role: "separator" as const,
      "aria-label": "Resize panel",
      tabIndex: 0,
    },
    isDesktop: true,
  }),
}));

vi.mock("./FilesPanel", () => ({
  FilesPanel: () => <div data-testid="files-panel-content" />,
}));

describe("FilesPanelDrawer resize handle geometry", () => {
  it("renders the handle as the panel's unclipped boundary sibling", () => {
    render(
      <FilesPanelDrawer
        open
        onClose={vi.fn()}
        onFileSelect={vi.fn()}
        flatView={false}
        showHidden={false}
        onShowHiddenChange={vi.fn()}
        sort="recent"
        onSortChange={vi.fn()}
      />,
    );
    const handle = screen.getByRole("separator", { name: "Resize panel" });
    const panel = screen.getByTestId("files-panel-drawer");

    expect(handle.nextElementSibling).toBe(panel);
    expect(panel.contains(handle)).toBe(false);
    expect(handle.closest(".overflow-hidden, .overflow-auto, .overflow-y-auto")).toBeNull();
    expect(handle.className).toMatch(/\bz-10\b/);
  });
});
