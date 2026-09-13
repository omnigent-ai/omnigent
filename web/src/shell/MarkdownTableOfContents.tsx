// Table of contents for markdown files, extracted from rendered heading elements.

import { useEffect, useMemo, useRef, useState } from "react";
import { SearchIcon, XIcon } from "lucide-react";
import { cn } from "@/lib/utils";

interface TocItem {
  id: string;
  text: string;
  level: number;
}

/**
 * Extract headings from the rendered markdown container. This reads the actual
 * DOM elements with their rehype-slug-generated IDs, ensuring TOC anchors match
 * what's actually rendered (including github-slugger's Unicode/emoji/inline-md
 * handling and any clobberPrefix from rehype-sanitize).
 */
function extractHeadings(container: HTMLElement | null): TocItem[] {
  if (!container) return [];

  const headings: TocItem[] = [];
  const headingElements = container.querySelectorAll("h1, h2, h3, h4, h5, h6");

  for (const el of headingElements) {
    const id = el.id;
    if (!id) continue; // Skip headings without IDs

    // Get text content, filtering out any raw HTML that wasn't properly rendered
    let text = el.textContent?.trim() ?? "";

    // If the text starts with '<', it's likely unrendered HTML - skip it
    if (text.startsWith("<")) continue;

    const level = parseInt(el.tagName[1], 10); // H1 -> 1, H2 -> 2, etc.

    headings.push({ id, text, level });
  }

  return headings;
}

interface MarkdownTableOfContentsProps {
  content: string;
  /** Ref to the scrollable container so TOC clicks can scroll to the target. */
  containerRef?: React.RefObject<HTMLElement | null>;
  /** Whether the TOC is open (for overlay mode). */
  open?: boolean;
  /** Callback when TOC should close. */
  onClose?: () => void;
}

export function MarkdownTableOfContents({
  content,
  containerRef,
  open = true,
  onClose,
}: MarkdownTableOfContentsProps) {
  const [headings, setHeadings] = useState<TocItem[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [filterText, setFilterText] = useState("");
  const searchInputRef = useRef<HTMLInputElement>(null);
  // While a TOC click is smooth-scrolling to its target, ignore the
  // IntersectionObserver so headings passing through the band don't steal the
  // active highlight from the one the user clicked. Holds the timestamp until
  // which observer updates are suppressed.
  const suppressObserverUntilRef = useRef(0);

  // Extract headings from rendered DOM after markdown renders
  useEffect(() => {
    const container = containerRef?.current;
    if (!container) {
      setHeadings([]);
      return;
    }

    // Wait for markdown to render, then extract headings from the DOM
    const updateHeadings = () => {
      setHeadings(extractHeadings(container));
    };

    // Give React time to render the markdown before initial extraction
    const timer = setTimeout(updateHeadings, 100);

    // Set up a mutation observer to catch any subsequent renders
    const observer = new MutationObserver(updateHeadings);
    observer.observe(container, { childList: true, subtree: true });

    return () => {
      clearTimeout(timer);
      observer.disconnect();
    };
  }, [containerRef, content]);

  // Track which heading is currently visible at the top of the viewport.
  useEffect(() => {
    const container = containerRef?.current;
    if (!container || headings.length === 0) return;

    const elements = headings
      .map((h) => container.querySelector(`#${CSS.escape(h.id)}`))
      .filter((el): el is Element => el !== null);
    if (elements.length === 0) return;

    // Order in document order so we can consistently pick the topmost heading
    // currently inside the observer band, regardless of entry callback order.
    const orderedIds = elements.map((el) => el.id);
    const visible = new Set<string>();

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) visible.add(entry.target.id);
          else visible.delete(entry.target.id);
        }
        // A programmatic scroll from a TOC click sets its target active up
        // front; don't let intermediate headings passing through the band
        // override it until the scroll settles.
        if (Date.now() < suppressObserverUntilRef.current) return;
        const topmost = orderedIds.find((id) => visible.has(id));
        if (topmost) setActiveId(topmost);
      },
      { root: container, rootMargin: "-20% 0px -80% 0px" },
    );

    for (const el of elements) observer.observe(el);
    return () => observer.disconnect();
  }, [headings, containerRef]);

  // Auto-focus search input when TOC opens
  useEffect(() => {
    if (open && searchInputRef.current) {
      searchInputRef.current.focus();
    }
  }, [open]);

  // Close on Escape key
  useEffect(() => {
    if (!open || !onClose) return;
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [open, onClose]);

  // Filter headings based on search text
  const filteredHeadings = useMemo(() => {
    if (!filterText.trim()) return headings;
    const lower = filterText.toLowerCase();
    return headings.filter((h) => h.text.toLowerCase().includes(lower));
  }, [headings, filterText]);

  if (headings.length === 0 || !open) return null;

  const handleClick = (id: string) => {
    const container = containerRef?.current;
    if (!container) return;
    const target = container.querySelector(`#${CSS.escape(id)}`);
    if (!target) return;

    // Get the element's position relative to the scrollable container
    const containerTop = container.getBoundingClientRect().top;
    const targetTop = target.getBoundingClientRect().top;
    const offset = targetTop - containerTop + container.scrollTop;

    // Highlight the clicked heading immediately and hold it through the smooth
    // scroll — otherwise headings passing through the observer band en route,
    // or a target too near the bottom to reach the band at all, would leave the
    // wrong section highlighted.
    setActiveId(id);
    suppressObserverUntilRef.current = Date.now() + 1000;

    // Scroll to position the heading near the top with a small margin
    container.scrollTo({ top: offset - 16, behavior: "smooth" });
    // Keep TOC open after clicking to allow quick navigation between sections
  };

  return (
    <nav
      className={cn(
        "sticky top-0 h-screen border-l border-border bg-card flex flex-col overflow-hidden",
      )}
      aria-label="Table of contents"
    >
      {/* Header with search */}
      <div className="shrink-0 border-b border-border p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-foreground">On this page</h2>
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              className="rounded p-1 hover:bg-muted transition-colors"
              aria-label="Close table of contents"
            >
              <XIcon className="size-4" />
            </button>
          )}
        </div>

        {/* Filter input */}
        <div className="relative">
          <SearchIcon className="absolute left-2.5 top-1/2 -translate-y-1/2 size-4 text-muted-foreground" />
          <input
            ref={searchInputRef}
            type="text"
            value={filterText}
            onChange={(e) => setFilterText(e.target.value)}
            placeholder="Filter headings"
            className="w-full rounded-md border border-border bg-background pl-9 pr-3 py-1.5 text-sm outline-none focus:border-primary focus:ring-1 focus:ring-primary"
          />
        </div>
      </div>

      {/* Scrollable headings list */}
      <div className="flex-1 overflow-y-auto px-4 py-2">
        {filteredHeadings.length === 0 ? (
          <p className="text-sm text-muted-foreground py-4 text-center">No matching headings</p>
        ) : (
          <ul className="space-y-1.5 text-sm">
            {filteredHeadings.map((heading) => (
              <li
                key={heading.id}
                style={{ paddingLeft: `${(heading.level - 1) * 0.75}rem` }}
                className="leading-snug"
              >
                <button
                  type="button"
                  onClick={() => handleClick(heading.id)}
                  className={cn(
                    "block w-full text-left transition-colors hover:text-foreground rounded px-2 py-1",
                    activeId === heading.id
                      ? "font-medium text-foreground bg-muted"
                      : "text-muted-foreground",
                  )}
                >
                  {heading.text}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </nav>
  );
}
