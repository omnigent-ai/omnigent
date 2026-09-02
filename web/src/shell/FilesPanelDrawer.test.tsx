import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActionsProvider, KeybindingDispatcher } from "@/actions";
import { FilesPanelDrawer } from "./FilesPanelDrawer";

vi.mock("@/hooks/useResizablePanel", () => ({
  useResizablePanel: () => ({ panelWidth: 400, handleProps: {}, isDesktop: false }),
}));
vi.mock("./FilesPanel", () => ({
  FilesPanel: () => (
    <textarea aria-label="nested search" onKeyDown={(event) => event.preventDefault()} />
  ),
}));

function renderDrawer(open: boolean, onClose = vi.fn()) {
  render(
    <ActionsProvider>
      <KeybindingDispatcher />
      <FilesPanelDrawer
        open={open}
        onClose={onClose}
        onFileSelect={() => {}}
        flatView={false}
        showHidden={false}
        onShowHiddenChange={() => {}}
        sort="recent"
        onSortChange={() => {}}
      />
    </ActionsProvider>,
  );
  return onClose;
}

afterEach(cleanup);

describe("FilesPanelDrawer actions", () => {
  it("closes an open drawer on Escape", () => {
    const onClose = renderDrawer(true);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("is inert while closed", () => {
    const onClose = renderDrawer(false);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("yields Escape to a nested widget that prevents default", () => {
    const onClose = renderDrawer(true);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "nested search" }), {
      key: "Escape",
    });
    expect(onClose).not.toHaveBeenCalled();
  });
});
