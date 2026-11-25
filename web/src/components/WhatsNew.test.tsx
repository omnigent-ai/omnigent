import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { parseEntries, WHATS_NEW_LAST_SEEN_KEY } from "@/whatsNew/whatsNew";
import { WhatsNew } from "./WhatsNew";

const today = new Date().toISOString().slice(0, 10);
const entries = parseEntries({
  [`./entries/${today}.md`]: "# Fresh things\n\n- **Shiny.** It works.",
});
const BADGE_TEXT = "See features released since your last visit.";

describe("WhatsNew", () => {
  beforeEach(() => window.localStorage.clear());

  it("shows the badge to a first-time visitor and opens the entries", () => {
    render(<WhatsNew entries={entries} />);
    fireEvent.click(screen.getByText(BADGE_TEXT));
    expect(screen.getByRole("dialog")).toHaveTextContent("Fresh things");
    expect(screen.queryByText(BADGE_TEXT)).toBeNull();
    expect(window.localStorage.getItem(WHATS_NEW_LAST_SEEN_KEY)).toBe(today);
  });

  it("dismisses without opening and stays hidden on the next load", () => {
    const { unmount } = render(<WhatsNew entries={entries} />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss What's New" }));
    expect(screen.queryByText(BADGE_TEXT)).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    unmount();
    render(<WhatsNew entries={entries} />);
    expect(screen.queryByText(BADGE_TEXT)).toBeNull();
  });

  it("sits at the bottom on phones when asked, keeping desktop placement", () => {
    const { rerender } = render(<WhatsNew entries={entries} />);
    const top = screen.getByTestId("whats-new");
    expect(top.className).toMatch(/(^| )top-\[calc\(4\.25rem/);
    rerender(<WhatsNew entries={entries} phoneAtBottom />);
    const bottom = screen.getByTestId("whats-new");
    expect(bottom.className).toMatch(/(^| )bottom-\[calc\(6\.5rem/);
    expect(bottom.className).not.toMatch(/(^| )top-\[/);
    expect(bottom.className).toContain("md:bottom-[calc(4.5rem");
    expect(bottom.className).toContain("md:left-3");
  });
});
