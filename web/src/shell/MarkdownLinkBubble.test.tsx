// Tests for MarkdownLinkBubble — the Open popup shown while the editor caret
// rests in a link. A real headless TipTap editor drives the selection; jsdom
// has no layout, so Range rects are stubbed to give coordsAtPos a position.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import { Markdown } from "@tiptap/markdown";
import { StarterKit } from "@tiptap/starter-kit";
import { FileViewerContext } from "./FileViewerContext";
import { MarkdownLinkBubble } from "./MarkdownLinkBubble";
import { ImageAwareLink } from "./TipTapWorkspaceImage";

const CONTENT =
  "See [ES-1](https://tracker.example.com/browse/ES-1), " +
  "[the plan](../notes/plan.md#steps), and [top](#top).";

const RECT = { left: 120, right: 160, top: 40, bottom: 56, width: 40, height: 16, x: 120, y: 40 };
// The editor's scroll viewport, which comfortably contains RECT.
const VIEWPORT_RECT = { left: 0, right: 800, top: 0, bottom: 600 };

const openFile = vi.fn();
const FILE_VIEWER = {
  openFile,
  openGithubTab: () => {},
  isChangedPath: () => false,
  conversationId: "conv_1",
  workspaceRoot: "/ws",
  workspaceHome: null,
};

let editor: Editor | null = null;
let containerElement: HTMLElement | null = null;

beforeEach(() => {
  openFile.mockClear();
  // jsdom implements neither method; coordsAtPos reads one of them.
  Range.prototype.getClientRects = () => [RECT] as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => RECT as DOMRect;
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  cleanup();
  editor?.destroy();
  containerElement?.remove();
  editor = null;
  containerElement = null;
});

function renderBubble({ editable = true, containerRect = VIEWPORT_RECT, content = CONTENT } = {}) {
  const container = document.createElement("div");
  container.getBoundingClientRect = () => containerRect as DOMRect;
  const editorElement = document.createElement("div");
  container.appendChild(editorElement);
  document.body.appendChild(container);
  containerElement = container;
  editor = new Editor({
    element: editorElement,
    editable,
    // The editor's own link setup, so links parse and render as in production.
    extensions: [
      StarterKit.configure({ link: false }),
      ImageAwareLink.configure({ openOnClick: false, autolink: false }),
      Markdown,
    ],
    content,
    contentType: "markdown",
  });
  render(
    <FileViewerContext.Provider value={FILE_VIEWER}>
      <MarkdownLinkBubble
        editor={editor}
        path="docs/guide.md"
        scrollContainerRef={{ current: container }}
      />
    </FileViewerContext.Provider>,
  );
  return editor;
}

/** Position `offset` characters into the first text node containing `text`. */
function posInText(ed: Editor, text: string, offset = 1): number {
  let found = -1;
  ed.state.doc.descendants((node, pos) => {
    if (found === -1 && node.isText && node.text?.includes(text)) {
      found = pos + node.text.indexOf(text) + offset;
    }
  });
  if (found === -1) throw new Error(`text not found: ${text}`);
  return found;
}

function placeCaret(ed: Editor, pos: number, to = pos) {
  act(() => {
    // view.focus() directly: the focus command defers to requestAnimationFrame.
    ed.view.focus();
    ed.commands.setTextSelection({ from: pos, to });
  });
}

const bubble = () => document.querySelector("[data-link-bubble]");

/** Give the popup a layout size; jsdom measures every element as 0×0. */
function mockPopupSize(width: number, height: number) {
  const measure = HTMLElement.prototype.getBoundingClientRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
    this: HTMLElement,
  ) {
    return this.hasAttribute("data-link-bubble")
      ? ({ width, height } as DOMRect)
      : measure.call(this);
  });
}

function resizeWindow(innerWidth: ReturnType<typeof vi.spyOn>, width: number) {
  innerWidth.mockReturnValue(width);
  act(() => {
    window.dispatchEvent(new Event("resize"));
  });
}

describe("MarkdownLinkBubble", () => {
  it("shows a web link's URL and an Open link that leaves the app window", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1"));
    expect(bubble()).not.toBeNull();
    expect(screen.getByText("https://tracker.example.com/browse/ES-1")).toBeDefined();
    const open = screen.getByRole("link", { name: "Open" });
    expect(open.getAttribute("href")).toBe("https://tracker.example.com/browse/ES-1");
    expect(open.getAttribute("target")).toBe("_blank");
    expect(open.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("sits below the link", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1", 2));
    const el = bubble() as HTMLElement;
    expect(el.style.top).toBe(`${RECT.bottom + 4}px`);
    expect(el.style.left).toBe(`${RECT.left}px`);
  });

  it("sits below the caret when the link wraps onto another line", () => {
    const line2 = { ...RECT, left: 24, right: 64, top: 60, bottom: 76, x: 24, y: 60 };
    // The link starts on line 1; from its third character on, it is on line 2.
    const rectFor = (range: Range) =>
      range.startContainer.textContent === "ES-1" && range.startOffset >= 2 ? line2 : RECT;
    Range.prototype.getClientRects = function (this: Range) {
      return [rectFor(this)] as unknown as DOMRectList;
    };
    Range.prototype.getBoundingClientRect = function (this: Range) {
      return rectFor(this) as DOMRect;
    };
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1", 3));
    const el = bubble() as HTMLElement;
    expect(el.style.top).toBe(`${line2.bottom + 4}px`);
    expect(el.style.left).toBe(`${line2.left}px`);
  });

  it("shifts left to stay inside the viewport, tracking window resizes", () => {
    const innerWidth = vi.spyOn(window, "innerWidth", "get").mockReturnValue(400);
    mockPopupSize(340, 30);
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1"));
    const left = () => (bubble() as HTMLElement).style.left;
    // 400 - 8 margin - 340 wide.
    expect(left()).toBe("52px");
    resizeWindow(innerWidth, 380);
    expect(left()).toBe("32px");
    // Back below the link once there is room.
    resizeWindow(innerWidth, 1000);
    expect(left()).toBe(`${RECT.left}px`);
  });

  it("flips above the link near the bottom of the viewport", () => {
    vi.spyOn(window, "innerHeight", "get").mockReturnValue(80);
    mockPopupSize(200, 20);
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1"));
    // Below would end at 56 + 4 + 20 = 80, past 80 - 8.
    expect((bubble() as HTMLElement).style.top).toBe(`${RECT.top - 4 - 20}px`);
  });

  it("shows while the caret is at the end of the link", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1", "ES-1".length));
    expect(screen.getByRole("link", { name: "Open" })).toBeDefined();
  });

  it("opens a workspace-relative link in the FileViewer", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "the plan"));
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(openFile).toHaveBeenCalledWith("notes/plan.md");
  });

  it("does not show outside a link", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "See"));
    expect(bubble()).toBeNull();
  });

  it("does not show for a range selection, which offers Add comment instead", () => {
    const ed = renderBubble();
    const from = posInText(ed, "ES-1", 0);
    placeCaret(ed, from, from + 3);
    expect(bubble()).toBeNull();
  });

  it.each(["javascript:alert(1)", "data:text/html,hi"])(
    "offers no Open for a disallowed destination (%s)",
    (href) => {
      const ed = renderBubble({ content: `Try [bad](${href}) now.` });
      placeCaret(ed, posInText(ed, "bad"));
      expect(bubble()).toBeNull();
    },
  );

  it("shows for a link at the start of a paragraph", () => {
    const ed = renderBubble({ content: "[ES-1](https://tracker.example.com/browse/ES-1) first." });
    placeCaret(ed, posInText(ed, "ES-1"));
    expect(screen.getByRole("link", { name: "Open" }).getAttribute("href")).toBe(
      "https://tracker.example.com/browse/ES-1",
    );
  });

  it("follows the link when the editor viewport resizes, as a panel drag does", () => {
    let onResize: (() => void) | undefined;
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: () => void) {
          onResize = callback;
        }
        observe() {}
        disconnect() {}
      },
    );
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1"));
    const moved = { ...RECT, left: 300, right: 340, x: 300 };
    Range.prototype.getClientRects = () => [moved] as unknown as DOMRectList;
    Range.prototype.getBoundingClientRect = () => moved as DOMRect;
    act(() => onResize?.());
    expect((bubble() as HTMLElement).style.left).toBe(`${moved.left}px`);
  });

  it.each([
    ["an in-page # link", "[top](#top)", "top"],
    ["a query-only link", "[raw](?raw=true)", "raw"],
  ])("does not show for %s, which names no file", (_label, link, text) => {
    const ed = renderBubble({ content: `See ${link} here.` });
    placeCaret(ed, posInText(ed, text));
    expect(bubble()).toBeNull();
  });

  it("hides when the link is outside the scroll container", () => {
    const ed = renderBubble({ containerRect: { ...VIEWPORT_RECT, top: 200 } });
    placeCaret(ed, posInText(ed, "ES-1"));
    expect(bubble()).toBeNull();
  });

  it("hides when a nested scroller, like a wide table's, scrolls the link out of view", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1"));
    expect(bubble()).not.toBeNull();
    const scroller = ed.view.dom.querySelector("p") as HTMLElement;
    scroller.style.overflowX = "auto";
    scroller.getBoundingClientRect = () => ({ ...VIEWPORT_RECT, left: 200 }) as DOMRect;
    act(() => {
      // Scroll events don't bubble.
      scroller.dispatchEvent(new Event("scroll"));
    });
    expect(bubble()).toBeNull();
  });

  it("hides when the editor loses focus", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1"));
    expect(bubble()).not.toBeNull();
    act(() => {
      (ed.view.dom as HTMLElement).blur();
    });
    expect(bubble()).toBeNull();
  });

  it("does not show in a read-only editor, where a plain click follows the link", () => {
    const ed = renderBubble({ editable: false });
    placeCaret(ed, posInText(ed, "ES-1"));
    expect(bubble()).toBeNull();
  });

  it("moves focus to Open on Alt+Enter, where Enter activates it", async () => {
    const user = userEvent.setup();
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "the plan"));
    await user.keyboard("{Alt>}{Enter}{/Alt}");
    const open = screen.getByRole("button", { name: "Open" });
    expect(document.activeElement).toBe(open);
    expect(open.getAttribute("aria-keyshortcuts")).toBe("Alt+Enter");
    await user.keyboard("{Enter}");
    expect(openFile).toHaveBeenCalledWith("notes/plan.md");
  });

  it("follows a web link from the keyboard: Alt+Enter, then Enter", async () => {
    const user = userEvent.setup();
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    // The popup-blocked fallback navigates through a temporary anchor.
    const followed: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      followed.push(this.href);
    });
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1"));
    await user.keyboard("{Alt>}{Enter}{/Alt}");
    expect(document.activeElement).toBe(screen.getByRole("link", { name: "Open" }));
    await user.keyboard("{Enter}");
    expect(openSpy).toHaveBeenCalledWith("about:blank", "_blank");
    expect(followed).toEqual(["https://tracker.example.com/browse/ES-1"]);
  });

  it("returns focus to the editor on Escape and stays open", async () => {
    const user = userEvent.setup();
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1"));
    await user.keyboard("{Alt>}{Enter}{/Alt}");
    expect(document.activeElement).toBe(screen.getByRole("link", { name: "Open" }));
    await user.keyboard("{Escape}");
    expect(document.activeElement).toBe(ed.view.dom);
    expect(bubble()).not.toBeNull();
  });

  it("hides when focus leaves both the editor and the popup", async () => {
    const user = userEvent.setup();
    const ed = renderBubble();
    const elsewhere = document.createElement("button");
    containerElement?.after(elsewhere);
    placeCaret(ed, posInText(ed, "ES-1"));
    await user.keyboard("{Alt>}{Enter}{/Alt}");
    expect(bubble()).not.toBeNull();
    act(() => {
      elsewhere.focus();
    });
    expect(bubble()).toBeNull();
    elsewhere.remove();
  });
});
