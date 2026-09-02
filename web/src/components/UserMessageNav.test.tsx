// Invariants:
// - hidden=true → returns null.
// - canPrev/canNext drive the `disabled` attribute (asserted explicitly
//   to catch a regression to aria-disabled, which wouldn't block clicks).

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setUserKeybindingRule } from "@/actions";
import { resetKeybindingStoreForTesting } from "@/actions/KeybindingStore";
import { TooltipProvider } from "@/components/ui/tooltip";
import { UserMessageNav } from "./UserMessageNav";

function renderNav(props: Partial<React.ComponentProps<typeof UserMessageNav>>) {
  const merged = {
    goPrev: vi.fn(),
    goNext: vi.fn(),
    canPrev: true,
    canNext: true,
    hidden: false,
    ...props,
  };
  render(
    <TooltipProvider>
      <UserMessageNav {...merged} />
    </TooltipProvider>,
  );
  return merged;
}

beforeEach(() => {
  localStorage.clear();
  resetKeybindingStoreForTesting();
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  resetKeybindingStoreForTesting();
});

describe("UserMessageNav", () => {
  it("renders nothing when hidden", () => {
    renderNav({ hidden: true });
    expect(screen.queryByLabelText("Previous user message")).toBeNull();
    expect(screen.queryByLabelText("Next user message")).toBeNull();
  });

  it("renders both buttons when there is content to navigate", () => {
    renderNav({});
    expect(screen.getByLabelText("Previous user message")).toBeEnabled();
    expect(screen.getByLabelText("Next user message")).toBeEnabled();
  });

  it("disables Previous when canPrev=false", () => {
    const props = renderNav({ canPrev: false });
    const btn = screen.getByLabelText("Previous user message");
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(props.goPrev).not.toHaveBeenCalled();
  });

  it("disables Next when canNext=false", () => {
    const props = renderNav({ canNext: false });
    const btn = screen.getByLabelText("Next user message");
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(props.goNext).not.toHaveBeenCalled();
  });

  it("invokes goPrev / goNext on click", () => {
    const props = renderNav({});
    fireEvent.click(screen.getByLabelText("Previous user message"));
    fireEvent.click(screen.getByLabelText("Next user message"));
    expect(props.goPrev).toHaveBeenCalledOnce();
    expect(props.goNext).toHaveBeenCalledOnce();
  });

  it("derives the live message-navigation hint without a rule-id dependency", async () => {
    expect(
      setUserKeybindingRule({
        id: "chat.openPreviousMessage",
        action: "chat.action.openPreviousMessage",
        sequence: null,
        mode: "global",
      }),
    ).toEqual({ ok: true, changed: true });
    renderNav({});
    fireEvent.focus(screen.getByLabelText("Previous user message"));
    expect(await screen.findByText("Previous message (Ctrl+Alt+Shift+↑)")).toBeInTheDocument();
  });

  it("forwards className to the control container", () => {
    // The connected wrapper passes responsive classes (e.g. `md:hidden`, so
    // the TurnRail minimap replaces these buttons on desktop) via className.
    // If the component dropped it, that mobile-only gating would silently
    // stop working. Assert the class reaches the rendered container.
    renderNav({ className: "md:hidden" });
    const container = screen.getByLabelText("Previous user message").closest("div");
    expect(container).toHaveClass("md:hidden");
  });
});
