import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { TerminalSurface } from "./ChatPage";

afterEach(cleanup);

describe("TerminalSurface", () => {
  it("hides via opacity-0/pointer-events-none/inert instead of the invisible class, and clears them when shown", () => {
    // jsdom cannot compute Tailwind styles or compositing, so this structurally guards against
    // reverting to `invisible`; scrollbar-bleed behavior belongs in e2e/Playwright.
    const { container, rerender } = render(<TerminalSurface isShown={false} />);
    const surface = container.firstChild as HTMLElement;

    expect(surface).toHaveClass("opacity-0", "pointer-events-none");
    expect(surface).not.toHaveClass("invisible");
    expect(surface).toHaveAttribute("inert");
    expect(surface).toHaveAttribute("aria-hidden", "true");

    rerender(<TerminalSurface isShown />);

    expect(surface).not.toHaveClass("opacity-0", "pointer-events-none", "invisible");
    expect(surface).not.toHaveAttribute("inert");
    expect(surface).toHaveAttribute("aria-hidden", "false");
  });
});
