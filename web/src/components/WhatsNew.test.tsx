import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { parseEntries, WHATS_NEW_LAST_SEEN_KEY } from "@/whatsNew/whatsNew";
import { loadWhatsNew, openWhatsNew, WhatsNew, WhatsNewSidebarCard } from "./WhatsNew";

const today = new Date().toISOString().slice(0, 10);
const entries = parseEntries({
  [`./entries/${today}.md`]: "# Fresh things\n\n- **Shiny.** It works.",
});

function renderWhatsNew(phoneAtBottom = false) {
  return render(
    <>
      <WhatsNewSidebarCard />
      <WhatsNew phoneAtBottom={phoneAtBottom} />
    </>,
  );
}

describe("WhatsNew", () => {
  beforeEach(() => {
    window.localStorage.clear();
    loadWhatsNew(entries);
  });

  it("shows the newest headline on the sidebar card and opens the notes", () => {
    renderWhatsNew();
    const card = screen.getByTestId("whats-new-card");
    expect(card).toHaveTextContent("Fresh things");
    fireEvent.click(screen.getByText("Fresh things"));
    expect(screen.getByRole("dialog")).toHaveTextContent("It works.");
    expect(screen.queryByTestId("whats-new-card")).toBeNull();
    expect(screen.queryByTestId("whats-new")).toBeNull();
    expect(window.localStorage.getItem(WHATS_NEW_LAST_SEEN_KEY)).toBe(today);
  });

  it("dismisses without opening and stays hidden on the next load", () => {
    const { unmount } = renderWhatsNew();
    fireEvent.click(screen.getAllByRole("button", { name: "Dismiss What's new" })[0]);
    expect(screen.queryByTestId("whats-new-card")).toBeNull();
    expect(screen.queryByTestId("whats-new")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    unmount();
    loadWhatsNew(entries);
    renderWhatsNew();
    expect(screen.queryByTestId("whats-new-card")).toBeNull();
  });

  it("reopens the notes after they were dismissed", () => {
    window.localStorage.setItem(WHATS_NEW_LAST_SEEN_KEY, today);
    loadWhatsNew(entries);
    renderWhatsNew();
    expect(screen.queryByTestId("whats-new-card")).toBeNull();
    act(() => openWhatsNew());
    expect(screen.getByRole("dialog")).toHaveTextContent("Fresh things");
  });

  it("keeps the phone pill off desktop and at the bottom of project pages", () => {
    const { unmount } = renderWhatsNew();
    const top = screen.getByTestId("whats-new");
    expect(top.className).toContain("md:hidden");
    expect(top.className).toMatch(/(^| )top-\[calc\(4\.25rem/);
    unmount();
    renderWhatsNew(true);
    const bottom = screen.getByTestId("whats-new");
    expect(bottom.className).toMatch(/(^| )bottom-\[calc\(6\.5rem/);
    expect(bottom.className).not.toMatch(/(^| )top-\[/);
  });

  it("shows nothing when the shell doesn't mount the notes", () => {
    render(<WhatsNewSidebarCard />);
    expect(screen.queryByTestId("whats-new-card")).toBeNull();
  });
});
