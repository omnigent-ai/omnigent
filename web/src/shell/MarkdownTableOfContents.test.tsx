import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor, fireEvent, act } from "@testing-library/react";
import { MarkdownTableOfContents } from "./MarkdownTableOfContents";
import { useRef, useEffect } from "react";

// The active-heading highlight is driven by an IntersectionObserver. jsdom's
// default stub in test-setup never fires, so tests that assert highlighting
// install a controllable observer whose callback they can invoke by hand.
function installControllableObserver() {
  let callback: IntersectionObserverCallback | undefined;
  const observed: Element[] = [];
  class TestObserver {
    constructor(cb: IntersectionObserverCallback) {
      callback = cb;
    }
    observe = (el: Element) => observed.push(el);
    unobserve = vi.fn();
    disconnect = vi.fn();
    takeRecords = () => [];
    root = null;
    rootMargin = "";
    thresholds = [];
  }
  vi.stubGlobal("IntersectionObserver", TestObserver);
  // Fire the observer with entries flagging each id as intersecting or not.
  const fire = (states: Record<string, boolean>) => {
    const entries = observed.map(
      (target) =>
        ({ target, isIntersecting: states[target.id] ?? false }) as IntersectionObserverEntry,
    );
    act(() => {
      callback?.(entries, {} as IntersectionObserver);
    });
  };
  return { fire };
}

// Renders the TOC over a DOM with three headings and a scrollable container.
function renderThreeHeadingToc() {
  function Wrapper() {
    const ref = useRef<HTMLDivElement>(null);
    useEffect(() => {
      if (ref.current) {
        ref.current.innerHTML = `
          <h1 id="alpha">Alpha</h1>
          <p>content</p>
          <h2 id="beta">Beta</h2>
          <p>content</p>
          <h2 id="gamma">Gamma</h2>
          <p>content</p>
        `;
      }
    }, []);
    return (
      <div>
        <div ref={ref} />
        <MarkdownTableOfContents content="# Alpha\n## Beta\n## Gamma" containerRef={ref} />
      </div>
    );
  }
  return render(<Wrapper />);
}

// The button whose text matches is "active" when it carries the bg-muted class.
function isActive(container: HTMLElement, text: string): boolean {
  const btn = [...container.querySelectorAll("nav button")].find((b) => b.textContent === text);
  return btn?.className.includes("bg-muted") ?? false;
}

describe("MarkdownTableOfContents", () => {
  it("should extract headings from rendered DOM", async () => {
    const content = `# Main Title
Some content here.

## Section 1
More content.

### Subsection 1.1
Details.

## Section 2
Final section.`;

    function Wrapper() {
      const ref = useRef<HTMLDivElement>(null);

      // Simulate rendered markdown by creating DOM structure
      useEffect(() => {
        if (ref.current) {
          ref.current.innerHTML = `
            <h1 id="main-title">Main Title</h1>
            <p>Some content here.</p>
            <h2 id="section-1">Section 1</h2>
            <p>More content.</p>
            <h3 id="subsection-11">Subsection 1.1</h3>
            <p>Details.</p>
            <h2 id="section-2">Section 2</h2>
            <p>Final section.</p>
          `;
        }
      }, []);

      return (
        <div>
          <div ref={ref} />
          <MarkdownTableOfContents content={content} containerRef={ref} />
        </div>
      );
    }

    const { container } = render(<Wrapper />);

    // Wait for the component to extract headings from the DOM
    await waitFor(() => {
      const nav = container.querySelector("nav");
      expect(nav).toBeInTheDocument();

      // Check that all headings are in the TOC (as buttons)
      const buttons = nav?.querySelectorAll("button");
      expect(buttons?.length).toBe(4);
    });

    // Verify the TOC contains the expected headings
    const nav = container.querySelector("nav");
    expect(nav?.textContent).toContain("Main Title");
    expect(nav?.textContent).toContain("Section 1");
    expect(nav?.textContent).toContain("Subsection 1.1");
    expect(nav?.textContent).toContain("Section 2");
  });

  it("should not render when there are no headings", () => {
    const content = "Just some plain text without any headings.";

    function Wrapper() {
      const ref = useRef<HTMLDivElement>(null);

      // Simulate rendered markdown with no headings
      useEffect(() => {
        if (ref.current) {
          ref.current.innerHTML = "<p>Just some plain text without any headings.</p>";
        }
      }, []);

      return (
        <div>
          <div ref={ref} />
          <MarkdownTableOfContents content={content} containerRef={ref} />
        </div>
      );
    }

    const { container } = render(<Wrapper />);
    expect(container.querySelector("nav")).not.toBeInTheDocument();
  });

  it("should handle duplicate heading IDs from rehype-slug", async () => {
    const content = `# Introduction
## Details
## Details
## Details`;

    function Wrapper() {
      const ref = useRef<HTMLDivElement>(null);

      // Simulate rehype-slug's duplicate handling: details, details-1, details-2
      useEffect(() => {
        if (ref.current) {
          ref.current.innerHTML = `
            <h1 id="introduction">Introduction</h1>
            <h2 id="details">Details</h2>
            <h2 id="details-1">Details</h2>
            <h2 id="details-2">Details</h2>
          `;
        }
      }, []);

      return (
        <div>
          <div ref={ref} />
          <MarkdownTableOfContents content={content} containerRef={ref} />
        </div>
      );
    }

    const { container } = render(<Wrapper />);

    // Wait for headings to be extracted
    await waitFor(() => {
      const buttons = container.querySelectorAll("button");
      expect(buttons.length).toBe(4);
    });
  });

  describe("active-section highlighting", () => {
    beforeEach(() => {
      vi.useFakeTimers();
      // jsdom doesn't implement scrollTo; the click handler calls it.
      Element.prototype.scrollTo = vi.fn();
    });
    afterEach(() => {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    });

    it("highlights the topmost visible heading when several are in the band", async () => {
      const { fire } = installControllableObserver();
      const { container } = renderThreeHeadingToc();

      // The initial extraction is behind a 100ms timer.
      act(() => {
        vi.advanceTimersByTime(150);
      });
      expect(container.querySelectorAll("nav button").length).toBe(3);

      // Beta and Gamma both intersect; entries are delivered gamma-first to
      // prove the choice is document order, not callback order.
      fire({ gamma: true, beta: true });

      expect(isActive(container, "Beta")).toBe(true);
      expect(isActive(container, "Gamma")).toBe(false);
    });

    it("keeps the clicked heading highlighted through the scroll", async () => {
      const { fire } = installControllableObserver();
      const { container } = renderThreeHeadingToc();

      act(() => {
        vi.advanceTimersByTime(150);
      });
      expect(container.querySelectorAll("nav button").length).toBe(3);

      const gammaBtn = [...container.querySelectorAll("nav button")].find(
        (b) => b.textContent === "Gamma",
      )!;
      act(() => {
        fireEvent.click(gammaBtn);
      });
      expect(isActive(container, "Gamma")).toBe(true);

      // A heading passing through the band mid-scroll must not steal the
      // highlight while the click's suppression window is open.
      fire({ alpha: true });
      expect(isActive(container, "Gamma")).toBe(true);
      expect(isActive(container, "Alpha")).toBe(false);

      // Once the suppression window elapses, the observer takes over again.
      await act(async () => {
        vi.advanceTimersByTime(1100);
      });
      fire({ alpha: true });
      expect(isActive(container, "Alpha")).toBe(true);
      expect(isActive(container, "Gamma")).toBe(false);
    });
  });
});
