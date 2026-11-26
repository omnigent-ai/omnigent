import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";

import { INEXACT_PAGE_NOTE, workspaceHistoryCommands } from "../workspaceCommands/model";
import { WorkspaceHistoryChevrons } from "./WorkspaceHistoryChevrons";

function renderChevrons(
  back: string | null,
  forward: string | null,
  waiting = false,
  inexactBack = false,
) {
  const onGo = vi.fn();
  const commands = workspaceHistoryCommands({
    back: { target: back, waitingForPage: false, inexactPage: inexactBack },
    forward: { target: forward, waitingForPage: waiting },
  });
  render(
    <TooltipProvider>
      <WorkspaceHistoryChevrons commands={commands} onGo={onGo} />
    </TooltipProvider>,
  );
  return onGo;
}

describe("WorkspaceHistoryChevrons", () => {
  it("steps back from ‹ and advertises its chord", () => {
    const onGo = renderChevrons("a.ts", null);
    const back = screen.getByRole("button", { name: "Back" });
    expect(back).not.toHaveAttribute("aria-disabled");
    expect(back.getAttribute("aria-keyshortcuts")).toBeTruthy();
    fireEvent.click(back);
    expect(onGo).toHaveBeenCalledWith("back");
  });

  it("keeps an unavailable › focusable but inert, so its tooltip can say why", async () => {
    const onGo = renderChevrons("a.ts", null, true);
    const forward = screen.getByRole("button", { name: "Forward" });
    expect(forward).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(forward);
    expect(onGo).not.toHaveBeenCalled();
    fireEvent.focus(forward);
    expect((await screen.findAllByText("Waiting for the page to load")).length).toBeGreaterThan(0);
  });

  it("names where an available step lands in its tooltip", async () => {
    renderChevrons("a.ts", null);
    fireEvent.focus(screen.getByRole("button", { name: "Back" }));
    expect((await screen.findAllByText("Back to a.ts")).length).toBeGreaterThan(0);
  });

  it("says when a step onto a page reloads it by URL on an older app", async () => {
    renderChevrons("x.test", null, false, true);
    fireEvent.focus(screen.getByRole("button", { name: "Back" }));
    expect((await screen.findAllByText(INEXACT_PAGE_NOTE)).length).toBeGreaterThan(0);
  });

  it("renders nothing without commands", () => {
    const { container } = render(<WorkspaceHistoryChevrons commands={[]} onGo={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});
