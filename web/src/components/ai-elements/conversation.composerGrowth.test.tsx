import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Conversation, ConversationContent } from "./conversation";

/** Install mutable jsdom metrics for the composer-growth scroll contract. */
function setScrollMetrics(
  el: HTMLElement,
  metrics: { scrollTop: number; scrollHeight: number; clientHeight: number },
) {
  Object.defineProperty(el, "scrollTop", {
    configurable: true,
    get: () => metrics.scrollTop,
    set: (value: number) => {
      metrics.scrollTop = value;
    },
  });
  Object.defineProperty(el, "scrollHeight", {
    configurable: true,
    get: () => metrics.scrollHeight,
  });
  Object.defineProperty(el, "clientHeight", {
    configurable: true,
    get: () => metrics.clientHeight,
  });
}

describe("ConversationContent composer-growth scroll extent", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("extends max scroll via --composer-growth without moving scrollTop", () => {
    const { container } = render(
      <div>
        <Conversation>
          <ConversationContent>
            <div data-testid="tail-message">last reply line</div>
          </ConversationContent>
        </Conversation>
      </div>,
    );

    const wrapper = container.firstElementChild as HTMLElement;
    const scroller = wrapper.querySelector('[role="log"] > div') as HTMLElement;
    const content = scroller.firstElementChild as HTMLElement;
    const extent = screen.getByTestId("composer-growth-scroll-extent");

    // Spacer is a scroll-container sibling of contentRef — not inside it — so
    // StickToBottom's content ResizeObserver does not re-pin on growth.
    expect(extent.parentElement).toBe(scroller);
    expect(content.contains(extent)).toBe(false);
    expect(extent).toHaveStyle({ height: "var(--composer-growth, 0px)" });

    const restingHeight = 1000;
    const viewport = 400;
    const pinnedTop = restingHeight - viewport;
    const metrics = {
      scrollTop: pinnedTop,
      scrollHeight: restingHeight,
      clientHeight: viewport,
    };
    setScrollMetrics(scroller, metrics);

    expect(metrics.scrollHeight - metrics.clientHeight - metrics.scrollTop).toBe(0);

    const growth = 72;
    wrapper.style.setProperty("--composer-growth", `${growth}px`);
    // Layout engines fold the spacer into scrollHeight; jsdom needs the same.
    metrics.scrollHeight = restingHeight + growth;

    // Pinned-at-bottom typing: extent grows, offset stays — no transcript jump.
    expect(metrics.scrollTop).toBe(pinnedTop);
    expect(metrics.scrollHeight - metrics.clientHeight).toBe(pinnedTop + growth);
    expect(metrics.scrollHeight - metrics.clientHeight - metrics.scrollTop).toBe(growth);

    // Manually scrolled: same contract — only the reachable range grows.
    metrics.scrollTop = 120;
    metrics.scrollHeight = restingHeight;
    wrapper.style.setProperty("--composer-growth", "0px");
    expect(metrics.scrollTop).toBe(120);

    wrapper.style.setProperty("--composer-growth", `${growth}px`);
    metrics.scrollHeight = restingHeight + growth;
    expect(metrics.scrollTop).toBe(120);
    expect(metrics.scrollHeight - metrics.clientHeight).toBe(restingHeight + growth - viewport);

    // Scroll-to-bottom can now land past the covered endpoint.
    scroller.scrollTop = metrics.scrollHeight - metrics.clientHeight;
    expect(metrics.scrollTop).toBe(pinnedTop + growth);
    expect(metrics.scrollHeight - metrics.clientHeight - metrics.scrollTop).toBe(0);
  });
});
