import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { FileIcon } from "lucide-react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { Tabs, TabsList } from "@/components/ui/tabs";

import { TAB_GAP, sharedTabWidth } from "./tabStripWidth";
import { useTabStripWidth } from "./useTabStripWidth";
import { StripTab } from "./WorkspaceStripTab";

const AREA_WIDTH = 428;
const RESERVED = 28;

/** A strip like the sidebar's, laid out by hand since jsdom has no layout. */
function Strip({ labels }: { labels: string[] }) {
  const [tabs, setTabs] = useState(labels);
  const stripWidth = useTabStripWidth(tabs.length, RESERVED);
  return (
    <div data-testid="strip" {...stripWidth.pointerHandlers}>
      <div ref={stripWidth.areaRef}>
        <Tabs value={tabs[0] ?? ""} activationMode="manual">
          <TabsList aria-label="Workspace tabs">
            {tabs.map((label, index) => (
              <StripTab
                key={label}
                tabId={label}
                index={index}
                title={{ label, tooltip: label, icon: FileIcon, connecting: false }}
                selected={index === 0}
                closable
                width={stripWidth.width}
                onClose={() => {
                  stripWidth.closedByPointer();
                  setTabs((current) => current.filter((other) => other !== label));
                }}
                onKeyDown={() => {}}
                menu={null}
              />
            ))}
          </TabsList>
        </Tabs>
      </div>
    </div>
  );
}

/** Where tab `index`'s close button ends, from the widths the strip gave its tabs. */
function closeButtonRight(index: number): number {
  const entries = Array.from(document.querySelectorAll<HTMLElement>("[data-tab-strip-entry]"));
  const widths = entries.map((entry) =>
    Number.parseFloat(entry.style.getPropertyValue("--tab-width")),
  );
  return widths.slice(0, index + 1).reduce((sum, width) => sum + width, 0) + index * TAB_GAP;
}

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(AREA_WIDTH);
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function renderStrip(labels: string[]) {
  render(
    <TooltipProvider>
      <Strip labels={labels} />
    </TooltipProvider>,
  );
}

describe("StripTab widths", () => {
  it("gives every tab, the selected one included, the same width whatever its title", () => {
    renderStrip(["a.ts", "a much longer file name than the others.tsx", "zsh · npm run build"]);
    const widths = Array.from(document.querySelectorAll<HTMLElement>("[data-tab-strip-entry]")).map(
      (entry) => entry.style.getPropertyValue("--tab-width"),
    );
    expect(new Set(widths)).toEqual(new Set([`${sharedTabWidth(AREA_WIDTH - RESERVED, 3)}px`]));
  });

  it("keeps the next tab's × under the pointer after a close, until the pointer leaves the strip", () => {
    renderStrip(["one", "two", "three", "four", "five"]);
    const strip = screen.getByTestId("strip");
    fireEvent.pointerEnter(strip);
    const before = closeButtonRight(1);
    fireEvent.click(screen.getByRole("button", { name: /Close two/ }));
    // "three" is now second; its × ends where "two"'s did.
    expect(closeButtonRight(1)).toBe(before);
    fireEvent.click(screen.getByRole("button", { name: /Close three/ }));
    expect(closeButtonRight(1)).toBe(before);
    act(() => {
      fireEvent.pointerLeave(strip);
    });
    expect(closeButtonRight(1)).toBeGreaterThan(before);
  });
});

describe("StripTab icon and divider", () => {
  function renderTabs(
    tabs: { label: string; filePath?: string }[],
    selectedIndex: number,
  ): HTMLElement[] {
    render(
      <TooltipProvider>
        <Tabs value={tabs[selectedIndex].label} activationMode="manual">
          <TabsList aria-label="Workspace tabs">
            {tabs.map((tab, index) => (
              <StripTab
                key={tab.label}
                tabId={tab.label}
                index={index}
                title={{ ...tab, tooltip: tab.label, icon: FileIcon, connecting: false }}
                selected={index === selectedIndex}
                closable
                width={120}
                onClose={() => {}}
                onKeyDown={() => {}}
                menu={null}
              />
            ))}
          </TabsList>
        </Tabs>
      </TooltipProvider>,
    );
    return Array.from(document.querySelectorAll<HTMLElement>("[data-tab-strip-entry]"));
  }

  it("shows a file tab's type icon in place of the generic one", () => {
    const [file, other] = renderTabs([{ label: "a.py", filePath: "a.py" }, { label: "Files" }], 0);
    expect(file.querySelector("svg[data-file-type-icon]")).not.toBeNull();
    expect(other.querySelector("svg[data-file-type-icon]")).toBeNull();
  });

  it("marks the selected tab, so the dividers beside it hide, and keeps every width", () => {
    const entries = renderTabs([{ label: "a" }, { label: "b" }, { label: "c" }], 1);
    expect(entries.map((entry) => entry.dataset.selected)).toEqual([undefined, "true", undefined]);
    // The divider is drawn by each entry's ::before and takes no width.
    for (const entry of entries) {
      expect(entry.className).toContain("before:absolute");
      expect(entry.style.getPropertyValue("--tab-width")).toBe("120px");
    }
    expect(entries[2].className).toContain(
      "[[data-tab-strip-entry][data-selected]+&]:before:opacity-0",
    );
    expect(entries[0].className).toContain("first:before:opacity-0");
  });

  describe("keyboard focus", () => {
    // jsdom never matches `:focus-visible`; here the browser shows focus wherever it is.
    beforeEach(() => {
      const matches = Element.prototype.matches;
      vi.spyOn(Element.prototype, "matches").mockImplementation(function (
        this: Element,
        selector: string,
      ) {
        if (selector === ":focus-visible") return document.activeElement === this;
        return matches.call(this, selector);
      });
    });

    const triggerOf = (entry: HTMLElement) =>
      entry.querySelector<HTMLElement>("[data-slot=tabs-trigger]")!;

    it("marks a tab focused from the keyboard, which rings it and hides the dividers beside it", () => {
      const entries = renderTabs([{ label: "a" }, { label: "b" }, { label: "c" }], 0);
      act(() => triggerOf(entries[1]).focus());
      expect(entries.map((entry) => entry.dataset.keyboardFocus)).toEqual([
        undefined,
        "true",
        undefined,
      ]);
      // The ring, its forced-colors outline and both dividers draw on that one mark.
      expect(entries[1].className).toContain("data-keyboard-focus:ring-2");
      expect(entries[1].className).toContain("forced-colors:data-keyboard-focus:outline-2");
      expect(entries[1].className).toContain("data-keyboard-focus:before:opacity-0");
      expect(entries[2].className).toContain("[[data-keyboard-focus]+&]:before:opacity-0");
      act(() => triggerOf(entries[1]).blur());
      expect(entries[1]).not.toHaveAttribute("data-keyboard-focus");
    });

    it("leaves a tab a pointer focused unmarked, until a key is pressed on it", () => {
      const entries = renderTabs([{ label: "a" }, { label: "b" }], 0);
      const trigger = triggerOf(entries[1]);
      act(() => {
        fireEvent.pointerDown(trigger);
        trigger.focus();
      });
      expect(entries[1]).not.toHaveAttribute("data-keyboard-focus");
      fireEvent.keyDown(trigger, { key: "Shift" });
      expect(entries[1]).toHaveAttribute("data-keyboard-focus", "true");
    });
  });
});

describe("a pinned StripTab", () => {
  it("shows only its icon, keeps its title and count for screen readers, and has no ×", () => {
    const onClose = vi.fn();
    render(
      <TooltipProvider>
        <Tabs value="Agents" activationMode="manual">
          <TabsList aria-label="Workspace tabs">
            <StripTab
              tabId="Agents"
              index={0}
              title={{
                label: "Agents",
                tooltip: "Agents",
                icon: FileIcon,
                badge: "3",
                connecting: false,
              }}
              pinned
              selected
              closable
              width={160}
              onClose={onClose}
              onKeyDown={() => {}}
              menu={null}
            />
          </TabsList>
        </Tabs>
      </TooltipProvider>,
    );
    const tab = screen.getByRole("tab", { name: "Agents 3" });
    const entry = tab.closest<HTMLElement>("[data-tab-strip-entry]")!;
    expect(entry).toHaveAttribute("data-tab-pinned");
    expect(entry.style.getPropertyValue("--tab-width")).toBe("28px");
    expect(tab).toHaveAttribute("title", "Agents 3");
    expect(screen.queryByRole("button", { name: /^Close/ })).toBeNull();
    // A middle click still closes it.
    fireEvent(tab, new MouseEvent("auxclick", { bubbles: true, button: 1 }));
    expect(onClose).toHaveBeenCalledWith("pointer");
  });
});
