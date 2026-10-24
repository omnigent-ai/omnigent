import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { EmbeddedProvider } from "@/lib/embedded";
import { HistoryNavigationButtons } from "./HistoryNavigationButtons";

const landed = () => ({ committed: Promise.resolve(), finished: Promise.resolve() });

/** The window's Navigation API over `length` entries; jsdom has none. */
class FakeNavigation extends EventTarget {
  length: number;
  index: number;
  back = vi.fn(() => this.moveTo(this.index - 1));
  forward = vi.fn(() => this.moveTo(this.index + 1));

  constructor(length: number, index: number) {
    super();
    this.length = length;
    this.index = index;
  }

  get canGoBack() {
    return this.index > 0;
  }

  get canGoForward() {
    return this.index < this.length - 1;
  }

  /** Make `index` the current entry, as a step or a link click would. */
  moveTo(index: number) {
    this.index = index;
    this.dispatchEvent(new Event("currententrychange"));
    return landed();
  }
}

function renderButtons({ navigation = new FakeNavigation(3, 1), embedded = false } = {}) {
  vi.stubGlobal("navigation", navigation);
  const tree = (
    <TooltipProvider>
      <HistoryNavigationButtons />
    </TooltipProvider>
  );
  render(embedded ? <EmbeddedProvider>{tree}</EmbeddedProvider> : tree);
  return navigation;
}

const back = () => screen.getByRole("button", { name: "Back" });
const forward = () => screen.getByRole("button", { name: "Forward" });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("HistoryNavigationButtons", () => {
  it("renders nothing without the Navigation API", () => {
    render(
      <TooltipProvider>
        <HistoryNavigationButtons />
      </TooltipProvider>,
    );
    expect(screen.queryByRole("button", { name: "Back" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Forward" })).toBeNull();
  });

  it("renders nothing when embedded in a host page", () => {
    renderButtons({ embedded: true });
    expect(screen.queryByRole("button", { name: "Back" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Forward" })).toBeNull();
  });

  it("wraps the pair in the class index.css widens the macOS title bar for", () => {
    renderButtons();
    expect(back().closest(".history-navigation-buttons")).toContainElement(forward());
  });

  it("greys out both with nowhere to go, keeping them focusable", () => {
    const navigation = renderButtons({ navigation: new FakeNavigation(1, 0) });
    expect(back()).toHaveAttribute("aria-disabled", "true");
    expect(forward()).toHaveAttribute("aria-disabled", "true");
    // `disabled` would drop keyboard focus when a focused button runs out of history.
    expect(back()).toBeEnabled();
    expect(forward()).toBeEnabled();

    fireEvent.click(back());
    fireEvent.click(forward());
    expect(navigation.back).not.toHaveBeenCalled();
    expect(navigation.forward).not.toHaveBeenCalled();
  });

  it("steps through the Navigation API and follows where it lands", () => {
    const navigation = renderButtons();
    fireEvent.click(back());
    expect(navigation.back).toHaveBeenCalledTimes(1);
    expect(back()).toHaveAttribute("aria-disabled", "true");
    expect(forward()).not.toHaveAttribute("aria-disabled");

    fireEvent.click(forward());
    fireEvent.click(forward());
    expect(navigation.forward).toHaveBeenCalledTimes(2);
    expect(back()).not.toHaveAttribute("aria-disabled");
    expect(forward()).toHaveAttribute("aria-disabled", "true");
  });

  it("follows history changes made elsewhere, such as a link click", () => {
    const navigation = renderButtons({ navigation: new FakeNavigation(1, 0) });
    act(() => {
      navigation.length = 2;
      navigation.moveTo(1);
    });
    expect(back()).not.toHaveAttribute("aria-disabled");
    expect(forward()).toHaveAttribute("aria-disabled", "true");
  });

  it("handles a refused or interrupted step", async () => {
    const navigation = renderButtons();
    const interrupted = () => Promise.reject(new DOMException("Interrupted", "AbortError"));
    navigation.back.mockImplementationOnce(() => ({
      committed: interrupted(),
      finished: interrupted(),
    }));
    fireEvent.click(back());
    // Vitest fails the run if either rejection goes unhandled.
    await act(() => Promise.resolve());
    expect(navigation.back).toHaveBeenCalledTimes(1);
  });
});
