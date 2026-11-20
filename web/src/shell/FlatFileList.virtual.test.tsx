// Exercises the real virtualizer, which the shared test setup replaces with a
// pass-through that renders every row and would hide windowing bugs.
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { FlatFileList } from "./FlatFileList";

vi.unmock("@tanstack/react-virtual");

const VIEWPORT_HEIGHT = 300;
const ROW_HEIGHT = 24;
const ROW_GAP = 2;
const HEADER_HEIGHT = 28;
// The rows filling the viewport plus overscan on each side, with some slack.
const MAX_MOUNTED_ROWS = 60;

// Both FilesPanel's section and FlatFileList's own fallback scroll this way.
const SCROLLER = ".overflow-y-auto";

const restorers: (() => void)[] = [];
function stub<Key extends keyof HTMLElement>(key: Key, descriptor: PropertyDescriptor): void {
  const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, key);
  Object.defineProperty(HTMLElement.prototype, key, { configurable: true, ...descriptor });
  restorers.push(() => {
    if (original) Object.defineProperty(HTMLElement.prototype, key, original);
    else Reflect.deleteProperty(HTMLElement.prototype, key);
  });
}
const isScroller = (element: HTMLElement) => element.matches(SCROLLER);
const isRow = (element: HTMLElement) => element.tagName === "LI" && "index" in element.dataset;
const isWindowedList = (element: HTMLElement) =>
  element.tagName === "UL" && element.style.height !== "";
const heightOf = (element: HTMLElement) => {
  if (isScroller(element)) return VIEWPORT_HEIGHT;
  if (isRow(element)) return ROW_HEIGHT;
  return isWindowedList(element) ? Number.parseFloat(element.style.height) : 0;
};

// Where a windowed list starts in the scroll content: below its repository
// header, if any, and every header and expanded list before it.
function contentTopOf(list: HTMLElement, scroller: HTMLElement): number {
  let top = 0;
  for (const group of scroller.querySelectorAll<HTMLElement>("[role=group]")) {
    top += HEADER_HEIGHT;
    const groupList = group.querySelector<HTMLElement>(":scope > ul");
    if (groupList === list) return top;
    if (groupList) top += heightOf(groupList);
  }
  return top;
}

beforeAll(() => {
  // jsdom does no layout: give the scroller a viewport and every row a height.
  stub("offsetHeight", {
    get(this: HTMLElement) {
      return heightOf(this);
    },
  });
  stub("offsetWidth", {
    get(this: HTMLElement) {
      return isScroller(this) ? 300 : 0;
    },
  });
  stub("getBoundingClientRect", {
    value(this: HTMLElement) {
      // Content moves up as the scroller scrolls.
      const scroller = this.closest<HTMLElement>(SCROLLER);
      const list = isRow(this) ? this.parentElement : this;
      const listTop = scroller && list && isWindowedList(list) ? contentTopOf(list, scroller) : 0;
      const rowTop = isRow(this) ? Number.parseFloat(this.style.top) || 0 : 0;
      const top = isScroller(this) ? 0 : listTop + rowTop - (scroller?.scrollTop ?? 0);
      const height = heightOf(this);
      return { top, left: 0, right: 300, bottom: top + height, width: 300, height, x: 0, y: top };
    },
  });
  stub("scrollTo", {
    value(this: HTMLElement, options: ScrollToOptions) {
      this.scrollTop = options.top ?? this.scrollTop;
      this.dispatchEvent(new Event("scroll"));
    },
  });
});

afterAll(() => {
  for (const restore of restorers.reverse()) restore();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function changedFile(path: string, repository_root?: string): WorkspaceChangedFile {
  return {
    path,
    name: path.split("/").at(-1) ?? path,
    repository_root,
    status: "modified",
    bytes: 10,
    modified_at: null,
    lines_added: 1,
    lines_removed: 0,
  };
}

function manyFiles(count: number, root?: string) {
  return Array.from({ length: count }, (_, index) =>
    changedFile(`${root ? `${root}/` : ""}src/file-${String(index).padStart(4, "0")}.ts`, root),
  );
}

const listProps = (files: WorkspaceChangedFile[]) => ({
  files,
  isLoading: false,
  isError: false,
  error: null,
  onFileSelect: vi.fn(),
  showHidden: false,
  onShowHidden: vi.fn(),
  searchQuery: "",
  sort: "alpha" as const,
  conversationId: "conv_abc",
});

function Harness({ files }: { files: WorkspaceChangedFile[] }) {
  const scroller = useRef<HTMLElement>(null);
  return (
    <section ref={scroller} data-testid="scroller" className="overflow-y-auto">
      <FlatFileList {...listProps(files)} scrollParentRef={scroller} />
    </section>
  );
}

const mountedRows = (root = "") =>
  screen.queryAllByRole("button", { name: new RegExp(`^${root}src/file-\\d+\\.ts$`) });

function scrollTo(scroller: HTMLElement, top: number) {
  act(() => {
    scroller.scrollTop = top;
    fireEvent.scroll(scroller);
  });
}

// jsdom never reports resizes; this lets a test deliver one to whoever watches an element.
function recordResizeObservers() {
  const observers: { targets: Set<Element>; notify: () => void }[] = [];
  class RecordingResizeObserver {
    private readonly targets = new Set<Element>();
    constructor(callback: ResizeObserverCallback) {
      observers.push({ targets: this.targets, notify: () => callback([], this) });
    }
    observe(target: Element) {
      this.targets.add(target);
    }
    unobserve(target: Element) {
      this.targets.delete(target);
    }
    disconnect() {
      this.targets.clear();
    }
  }
  vi.stubGlobal("ResizeObserver", RecordingResizeObserver);
  return (element: Element) =>
    act(() => {
      for (const { targets, notify } of observers) if (targets.has(element)) notify();
    });
}

describe("FlatFileList windowing", () => {
  it("mounts only the rows near the viewport of a large change set", () => {
    const count = 2_000;
    render(<Harness files={manyFiles(count)} />);

    expect(mountedRows().length).toBeLessThan(MAX_MOUNTED_ROWS);
    expect(screen.getByRole("button", { name: "src/file-0000.ts" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "src/file-1999.ts" })).toBeNull();
    // The list keeps the full height, so the scrollbar still spans every row.
    const list = screen.getByRole("button", { name: "src/file-0000.ts" }).closest("ul");
    expect(list).toHaveStyle({ height: `${count * ROW_HEIGHT + (count - 1) * ROW_GAP}px` });

    scrollTo(screen.getByTestId("scroller"), 1_000 * (ROW_HEIGHT + ROW_GAP));

    expect(mountedRows().length).toBeLessThan(MAX_MOUNTED_ROWS);
    expect(screen.getByRole("button", { name: "src/file-1000.ts" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "src/file-0000.ts" })).toBeNull();
  });

  it("windows against its own scroll container when no scroll parent is given", () => {
    const { container } = render(<FlatFileList {...listProps(manyFiles(2_000))} />);

    expect(mountedRows().length).toBeLessThan(MAX_MOUNTED_ROWS);
    expect(screen.getByRole("button", { name: "src/file-0000.ts" })).toBeInTheDocument();

    const scroller = container.querySelector<HTMLElement>(SCROLLER);
    expect(scroller).not.toBeNull();
    scrollTo(scroller!, 1_000 * (ROW_HEIGHT + ROW_GAP));

    expect(mountedRows().length).toBeLessThan(MAX_MOUNTED_ROWS);
    expect(screen.getByRole("button", { name: "src/file-1000.ts" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "src/file-0000.ts" })).toBeNull();
  });

  it("windows each repository group at its own offset in the panel", () => {
    const perGroup = 1_000;
    render(<Harness files={[...manyFiles(perGroup, "api"), ...manyFiles(perGroup, "web")]} />);

    expect(screen.getByRole("button", { name: "api, 1000 changed files" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "web, 1000 changed files" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "api/src/file-0000.ts" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "api/src/file-0999.ts" })).toBeNull();
    expect(screen.queryByRole("button", { name: "web/src/file-0999.ts" })).toBeNull();
    expect(mountedRows("api/").length + mountedRows("web/").length).toBeLessThan(MAX_MOUNTED_ROWS);

    // Center the viewport on the boundary between the two groups.
    const apiListHeight = perGroup * ROW_HEIGHT + (perGroup - 1) * ROW_GAP;
    const webListTop = 2 * HEADER_HEIGHT + apiListHeight;
    scrollTo(screen.getByTestId("scroller"), webListTop - VIEWPORT_HEIGHT / 2);

    expect(screen.getByRole("button", { name: "api/src/file-0999.ts" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "web/src/file-0000.ts" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "api/src/file-0000.ts" })).toBeNull();
    expect(screen.queryByRole("button", { name: "web/src/file-0999.ts" })).toBeNull();
    expect(mountedRows("api/").length + mountedRows("web/").length).toBeLessThan(MAX_MOUNTED_ROWS);
  });

  it("moves a later repository's rows when an earlier one collapses or reopens", () => {
    const reportResize = recordResizeObservers();
    const perGroup = 1_000;
    render(<Harness files={[...manyFiles(perGroup, "api"), ...manyFiles(perGroup, "web")]} />);
    const scroller = screen.getByTestId("scroller");
    const groups = scroller.querySelector(":scope > ul")!;
    const apiHeader = screen.getByRole("button", { name: "api, 1000 changed files" });

    fireEvent.click(apiHeader);
    reportResize(groups);
    // With api collapsed, web's row 500 sits just below both headers.
    scrollTo(scroller, 2 * HEADER_HEIGHT + 500 * (ROW_HEIGHT + ROW_GAP) - VIEWPORT_HEIGHT / 2);
    expect(screen.getByRole("button", { name: "web/src/file-0500.ts" })).toBeInTheDocument();
    expect(mountedRows("api/")).toHaveLength(0);

    // Reopening api pushes web far below the scroll position, which stays put.
    const offset = scroller.scrollTop;
    fireEvent.click(apiHeader);
    reportResize(groups);
    expect(scroller.scrollTop).toBe(offset);
    expect(screen.getByRole("button", { name: "api/src/file-0500.ts" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "web/src/file-0500.ts" })).toBeNull();
    expect(mountedRows("api/").length + mountedRows("web/").length).toBeLessThan(MAX_MOUNTED_ROWS);
  });
});
