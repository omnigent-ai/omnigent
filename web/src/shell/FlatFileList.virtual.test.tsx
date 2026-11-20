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
// The rows filling the viewport plus overscan on each side, with some slack.
const MAX_MOUNTED_ROWS = 60;

const restorers: (() => void)[] = [];
function stub<Key extends keyof HTMLElement>(key: Key, descriptor: PropertyDescriptor): void {
  const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, key);
  Object.defineProperty(HTMLElement.prototype, key, { configurable: true, ...descriptor });
  restorers.push(() => {
    if (original) Object.defineProperty(HTMLElement.prototype, key, original);
    else Reflect.deleteProperty(HTMLElement.prototype, key);
  });
}
const isScroller = (element: HTMLElement) => element.dataset.testid === "scroller";
const heightOf = (element: HTMLElement) =>
  isScroller(element) ? VIEWPORT_HEIGHT : element.tagName === "LI" ? ROW_HEIGHT : 0;

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
      const scroller = this.closest<HTMLElement>("[data-testid=scroller]");
      const top = isScroller(this) ? 0 : -(scroller?.scrollTop ?? 0);
      const height = heightOf(this);
      return { top, left: 0, right: 300, bottom: top + height, width: 300, height, x: 0, y: top };
    },
  });
});

afterAll(() => {
  for (const restore of restorers.reverse()) restore();
});

afterEach(cleanup);

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

function Harness({ files }: { files: WorkspaceChangedFile[] }) {
  const scroller = useRef<HTMLElement>(null);
  return (
    <section ref={scroller} data-testid="scroller" style={{ overflowY: "auto" }}>
      <FlatFileList
        files={files}
        isLoading={false}
        isError={false}
        error={null}
        onFileSelect={vi.fn()}
        showHidden={false}
        onShowHidden={vi.fn()}
        searchQuery=""
        sort="alpha"
        conversationId="conv_abc"
        scrollParentRef={scroller}
      />
    </section>
  );
}

const mountedRows = () => screen.queryAllByRole("button", { name: /src\/file-\d+\.ts$/ });

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

    const scroller = screen.getByTestId("scroller");
    act(() => {
      scroller.scrollTop = 1_000 * (ROW_HEIGHT + ROW_GAP);
      fireEvent.scroll(scroller);
    });

    expect(mountedRows().length).toBeLessThan(MAX_MOUNTED_ROWS);
    expect(screen.getByRole("button", { name: "src/file-1000.ts" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "src/file-0000.ts" })).toBeNull();
  });

  it("windows the rows of each repository group", () => {
    render(<Harness files={[...manyFiles(1_000, "api"), ...manyFiles(1_000, "web")]} />);

    expect(screen.getByRole("button", { name: "api, 1000 changed files" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "web, 1000 changed files" })).toBeInTheDocument();
    expect(mountedRows().length).toBeLessThan(2 * MAX_MOUNTED_ROWS);
    expect(screen.queryByRole("button", { name: "api/src/file-0999.ts" })).toBeNull();
  });
});
