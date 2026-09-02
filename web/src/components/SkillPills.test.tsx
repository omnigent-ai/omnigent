// Regression tests for the skill pill tooltip's light-mode legibility.
//
// The default tooltip surface is inverted in light mode — a near-black chip
// with white text — and only becomes a light glass panel under `dark:`. This
// bubble carries two-level content (name + description) and styled that
// hierarchy with the page-surface tokens --foreground / --muted-foreground,
// which put dark text on the dark chip: 1.23:1 for the name and 3.80:1 for the
// description. Dark mode looked correct either way, because --foreground and
// --popover-foreground resolve to the same near-white there, which is why the
// bug shipped unnoticed.
//
// The fix takes the card surface instead of the chip, mirroring the sidebar's
// session tooltip (shell/Sidebar.tsx) — the app's only other multi-line bubble.
// Short one-line tooltips keep the chip.
//
// jsdom can't compute Tailwind styles, so contrast isn't directly testable;
// these tests pin the class-level invariant that produced the bug.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SkillPills } from "./SkillPills";
import { TooltipProvider } from "@/components/ui/tooltip";

afterEach(cleanup);

const SKILLS = [
  {
    name: "cross-review",
    description: "Verify an implementer's diff with an independent sub-agent.",
  },
  { name: "fanout", description: "Run the same prompt across several sub-agents." },
];

function renderPills(onPick = vi.fn()) {
  // delayDuration 0: the bubble mounts on the first hover/focus, no timers to
  // advance.
  render(
    <TooltipProvider delayDuration={0}>
      <SkillPills skills={SKILLS} onPick={onPick} />
    </TooltipProvider>,
  );
  return onPick;
}

/** The open bubble's own element (radix also mounts a visually-hidden copy). */
function bubble(): HTMLElement {
  const els = document.querySelectorAll<HTMLElement>('[data-slot="tooltip-content"]');
  expect(els.length).toBeGreaterThan(0);
  return els[0];
}

describe("SkillPills", () => {
  it("renders one pill per skill and hands the bare name to onPick", () => {
    const onPick = renderPills();
    expect(screen.getByTestId("skill-pill-cross-review").textContent).toBe("/cross-review");
    expect(screen.getByTestId("skill-pill-fanout").textContent).toBe("/fanout");
    fireEvent.click(screen.getByTestId("skill-pill-cross-review"));
    // Bare name, no leading slash — the caller prefills the composer with it.
    expect(onPick).toHaveBeenCalledWith("cross-review");
  });

  it("renders nothing when the agent bundles no skills", () => {
    render(
      <TooltipProvider>
        <SkillPills skills={[]} onPick={vi.fn()} />
      </TooltipProvider>,
    );
    expect(screen.queryByTestId("skill-pills")).toBeNull();
  });

  it("shows the name and description once the pill is focused", () => {
    renderPills();
    expect(screen.queryByText(SKILLS[0].description)).toBeNull();
    fireEvent.focus(screen.getByTestId("skill-pill-cross-review"));
    expect(bubble().textContent).toContain("/cross-review");
    expect(bubble().textContent).toContain(SKILLS[0].description);
  });

  // The guards below are the bug itself. The bubble styles its two lines with
  // page-surface text tokens, so it MUST also take the page-surface background;
  // inheriting the chip is what made the text unreadable in light mode.
  it("takes the card surface so its page-surface text tokens land on a light background", () => {
    renderPills();
    fireEvent.focus(screen.getByTestId("skill-pill-cross-review"));
    const cls = bubble().className;
    expect(cls).toContain("bg-popover");
    expect(cls).toContain("text-popover-foreground");
  });

  it("does not fall back to the inverted chip surface", () => {
    renderPills();
    fireEvent.focus(screen.getByTestId("skill-pill-cross-review"));
    // bg-neutral-900 is the tooltip default this bubble opts out of. If it ever
    // reappears here, the dark description text is back on a near-black box.
    expect(bubble().className).not.toMatch(/(^|\s)bg-neutral-\d+(\s|$)/);
  });

  it("keeps the description dimmed for hierarchy", () => {
    renderPills();
    fireEvent.focus(screen.getByTestId("skill-pill-cross-review"));
    const desc = screen
      .getAllByText(SKILLS[0].description)
      .find((el) => el.tagName === "P" && el.closest('[data-slot="tooltip-content"]'));
    expect(desc).toBeTruthy();
    // Legible on the card in both themes: 4.83:1 in light, 6.54:1 in dark.
    expect(desc?.className).toContain("text-muted-foreground");
  });
});
