// A selection's blocked-open notice, through the real Toaster: each batch keeps its own recovery
// across its retries, and another batch's outcome never takes it away.

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Toaster, toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as OpenLinks from "@/lib/openLinkInApp";

import { openSelectionLinks } from "./useSelectionLinkActions";

const outcome = vi.hoisted(() =>
  vi.fn<(conversationId: string, href: string) => "routed" | "opened" | "blocked">(),
);
vi.mock("@/lib/openLinkInApp", async (original) => ({
  ...(await original<typeof OpenLinks>()),
  openLinkWithOutcome: outcome,
}));

const linkTo = (number: number) => {
  const url = `https://github.com/acme/app/pull/${number}`;
  return { url, href: url };
};
/** The notices still shown: a dismissed one stays in the document, marked removed. */
const shownNotices = () =>
  [...document.querySelectorAll("[data-sonner-toast]:not([data-removed='true'])")].map(
    (notice) => notice.textContent,
  );
const allTwoBlocked =
  "Your browser blocked all 2 pull requests from opening. Allow pop-ups for this site, then try again.";
const oneBlocked =
  "Your browser blocked the pull request from opening. Allow pop-ups for this site, then try again.";
/** Waits out the Toaster's close animation, after which a dismissed toast is marked removed. */
const settle = () =>
  act(async () => {
    await new Promise((resolve) => {
      setTimeout(resolve, 500);
    });
  });

beforeEach(() => {
  outcome.mockReset().mockReturnValue("blocked");
  render(<Toaster />);
});
afterEach(() => {
  toast.dismiss();
  cleanup();
});

describe("openSelectionLinks's blocked notice", () => {
  it("stays, with the new count, when Open the Rest is blocked again, and goes once they open", async () => {
    outcome.mockImplementation((_conversation, href) =>
      href === linkTo(1).href ? "opened" : "blocked",
    );
    act(() => openSelectionLinks("conv_1", [linkTo(1), linkTo(2), linkTo(3)]));
    fireEvent.click(await screen.findByRole("button", { name: "Open the Rest" }));
    await settle();
    expect(shownNotices()).toEqual([expect.stringContaining(allTwoBlocked)]);
    expect(screen.getByRole("button", { name: "Try Again" })).toBeVisible();
    expect(outcome.mock.calls.slice(3)).toEqual([
      ["conv_1", linkTo(2).href],
      ["conv_1", linkTo(3).href],
    ]);

    outcome.mockReturnValue("opened");
    fireEvent.click(screen.getByRole("button", { name: "Try Again" }));
    await settle();
    expect(shownNotices()).toEqual([]);
  });

  it("keeps each batch's own, whatever another batch comes to", async () => {
    act(() => openSelectionLinks("conv_1", [linkTo(1), linkTo(2)]));
    act(() => openSelectionLinks("conv_1", [linkTo(5)]));
    outcome.mockReturnValue("opened");
    act(() => openSelectionLinks("conv_2", [linkTo(8)]));
    await settle();
    expect(shownNotices()).toEqual(
      expect.arrayContaining([
        expect.stringContaining(allTwoBlocked),
        expect.stringContaining(oneBlocked),
      ]),
    );
    expect(shownNotices()).toHaveLength(2);

    // Retrying one opens just its own and leaves the other's.
    outcome.mockClear();
    const firstNotice = screen.getByText(allTwoBlocked).closest("[data-sonner-toast]")!;
    fireEvent.click(within(firstNotice as HTMLElement).getByRole("button", { name: "Try Again" }));
    await settle();
    expect(outcome.mock.calls.map(([, href]) => href)).toEqual([linkTo(1).href, linkTo(2).href]);
    expect(shownNotices()).toEqual([expect.stringContaining(oneBlocked)]);
  });
});
