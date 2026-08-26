import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MobilePanelDrawer } from "./MobilePanelDrawer";

afterEach(cleanup);

function renderDrawer(props: Partial<Parameters<typeof MobilePanelDrawer>[0]> = {}) {
  return render(
    <MobilePanelDrawer
      open
      title="Shells"
      onClose={() => {}}
      testId="shells-panel-drawer"
      {...props}
    >
      <div data-testid="drawer-body" />
    </MobilePanelDrawer>,
  );
}

describe("MobilePanelDrawer", () => {
  it("carries the safe-area hook class so the header clears the notch", () => {
    // The drawer is a `fixed inset-0` overlay on phones. Without this class
    // the native shells' inset rule (index.css) misses it and the title +
    // Close button render under the status bar / dynamic island — leaving no
    // way to dismiss the panel.
    renderDrawer();
    expect(screen.getByTestId("shells-panel-drawer")).toHaveClass("mobile-panel-drawer");
  });

  it("keeps the safe-area class while closed", () => {
    // The class must not be state-dependent: the drawer animates in from the
    // right, so the inset has to be in place before it is on screen.
    renderDrawer({ open: false });
    expect(screen.getByTestId("shells-panel-drawer")).toHaveClass("mobile-panel-drawer");
  });

  it("renders the title and a working Close button", () => {
    const onClose = vi.fn();
    renderDrawer({ onClose });

    expect(screen.getByRole("heading", { name: "Shells" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("mounts children only while open", () => {
    const { rerender } = renderDrawer({ open: false });
    expect(screen.queryByTestId("drawer-body")).toBeNull();

    rerender(
      <MobilePanelDrawer open title="Shells" onClose={() => {}} testId="shells-panel-drawer">
        <div data-testid="drawer-body" />
      </MobilePanelDrawer>,
    );
    expect(screen.getByTestId("drawer-body")).toBeInTheDocument();
  });
});
