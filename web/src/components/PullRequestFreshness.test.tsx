import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";

import { PullRequestFreshness } from "./PullRequestFreshness";

const NOW = new Date(2026, 10, 19, 15, 0).getTime();

beforeEach(() => {
  vi.useFakeTimers({ now: NOW, toFake: ["Date"] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("PullRequestFreshness", () => {
  it("reads the kept data's age, with the exact time as its tooltip", () => {
    render(
      <PullRequestFreshness
        state={{ updatedAt: NOW - 12 * 60_000, refreshing: false, failed: false }}
      />,
    );
    const mark = screen.getByText("Updated 12 min ago");
    expect(mark.getAttribute("data-pull-request-freshness")).toBe("rest");
    expect(mark.getAttribute("title")).toMatch(/^Updated /);
  });

  it("says it's refreshing in words, without motion", () => {
    render(
      <PullRequestFreshness
        state={{ updatedAt: NOW - 12 * 60_000, refreshing: true, failed: false }}
      />,
    );
    const mark = screen.getByText("Updated 12 min ago · refreshing");
    expect(mark.getAttribute("data-pull-request-freshness")).toBe("refreshing");
    expect(mark.className).not.toMatch(/animate/);
  });

  it("says a refresh didn't go through", () => {
    render(
      <PullRequestFreshness
        state={{ updatedAt: NOW - 3 * 3_600_000, refreshing: false, failed: true }}
      />,
    );
    expect(screen.getByText("Updated 3 hr ago · couldn't refresh")).toBeTruthy();
  });

  it("shows nothing for fresh data or none", () => {
    const { container, rerender } = render(<PullRequestFreshness state={null} />);
    expect(container.textContent).toBe("");
    rerender(
      <PullRequestFreshness state={{ updatedAt: NOW - 10_000, refreshing: true, failed: false }} />,
    );
    expect(container.textContent).toBe("");
  });

  it("says only the age inside a control that already says what it updates", () => {
    render(
      <PullRequestFreshness
        state={{ updatedAt: NOW - 12 * 60_000, refreshing: true, failed: false }}
        form="age"
        id="list-age"
      />,
    );
    const mark = screen.getByText("12 min ago · refreshing");
    expect(mark.id).toBe("list-age");
    expect(mark.textContent).not.toMatch(/Updated/);
  });

  it("is a clock that says what it dates once the data is old enough", () => {
    const { rerender, container } = render(
      <PullRequestFreshness
        state={{ updatedAt: NOW - 14 * 60_000, refreshing: false, failed: false }}
        form="clock"
        showAfter={15 * 60_000}
        describe={(age) => `Reviewers as of ${age}`}
      />,
      { wrapper: TooltipProvider },
    );
    expect(container.textContent).toBe("");
    expect(screen.queryByRole("img")).toBeNull();
    rerender(
      <PullRequestFreshness
        state={{ updatedAt: NOW - 40 * 60_000, refreshing: false, failed: false }}
        form="clock"
        showAfter={15 * 60_000}
        describe={(age) => `Reviewers as of ${age}`}
      />,
    );
    expect(screen.getByRole("img", { name: "Reviewers as of 40 min ago" })).toBeTruthy();
    // No words beside the glyph: its tooltip and name say it.
    expect(container.textContent).toBe("");
  });
});
