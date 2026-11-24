import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { EditorView } from "@codemirror/view";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  onTestFinished,
  vi,
} from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { OWNS_COMMAND_ENTER_ATTRIBUTE } from "@/hooks/useApproveHotkey";
import { getHighlighter } from "@/components/ai-elements/code-block";
import type { Comment } from "@/hooks/useComments";
import { FILE_EDITOR_COMMANDS, fileEditorCommandLabel } from "@/components/fileEditorCommands";
import { CodeMirrorFileViewer, type CodeMirrorFileViewerProps } from "./CodeMirrorFileViewer";
import { getSavedScrollTop } from "../useScrollRestore";
import { setCommentLayer } from "./commentHighlighting";
import { isCommandListed, usePaletteCommandsStore } from "@/store/paletteCommands";
import { useChatStore } from "@/store/chatStore";

// jsdom has no layout; CodeMirror measures ranges when it scrolls a match into view.
const EMPTY_RECT = { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0, x: 0, y: 0 };
const originalRangeRects = {
  getClientRects: Range.prototype.getClientRects,
  getBoundingClientRect: Range.prototype.getBoundingClientRect,
};
beforeAll(() => {
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => EMPTY_RECT as DOMRect;
});
afterAll(() => Object.assign(Range.prototype, originalRangeRects));
afterEach(cleanup);

// Shiki loads slower than waitFor's default second, as it does on a busy
// machine, so highlighting assertions can't depend on how fast it loads.
vi.mock("shiki", async (importOriginal) => {
  await new Promise((resolve) => {
    setTimeout(resolve, 1_500);
  });
  return importOriginal();
});
// Load the engine and grammar up front; the tests check decorations, not load time.
beforeAll(() => getHighlighter("typescript"), 30_000);

// Two "alpha", "beta" pairs with Windows line breaks.
const CRLF_PAIRS = "alpha\r\nbeta\r\nalpha\r\nbeta";

const SOURCE = ["const alpha = 1;", "const beta = alpha;", "export { alpha, beta };"].join("\n");

function viewerProps(
  overrides: Partial<CodeMirrorFileViewerProps> = {},
): CodeMirrorFileViewerProps {
  return {
    conversationId: "conv_1",
    path: "src/values.ts",
    content: SOURCE,
    truncated: false,
    wrapLines: false,
    searchOpen: false,
    onSearchHandled: () => {},
    onSearchOpened: () => {},
    ...overrides,
  };
}

function renderViewer(overrides: Partial<CodeMirrorFileViewerProps> = {}) {
  const props = viewerProps(overrides);
  const result = render(
    <TooltipProvider>
      <CodeMirrorFileViewer {...props} />
    </TooltipProvider>,
  );
  const rerender = (next: Partial<CodeMirrorFileViewerProps>) =>
    result.rerender(
      <TooltipProvider>
        <CodeMirrorFileViewer {...props} {...next} />
      </TooltipProvider>,
    );
  return { ...result, rerender };
}

function lines(container: HTMLElement): string[] {
  return [...container.querySelectorAll(".cm-line")].map((line) => line.textContent ?? "");
}

describe("CodeMirrorFileViewer", () => {
  it("shows the file read-only with line numbers", () => {
    const { container } = renderViewer();
    expect(lines(container)).toEqual(SOURCE.split("\n"));
    expect(screen.getByRole("textbox", { name: "Contents of src/values.ts" })).toHaveAttribute(
      "aria-readonly",
      "true",
    );
    expect(container.querySelector(".cm-lineNumbers")).toHaveTextContent("123");
  });

  it("highlights syntax with the GitHub themes' token colors in both modes", async () => {
    const { container } = renderViewer();
    await waitFor(() => expect(container.querySelector(".cm-shiki-token")).not.toBeNull());
    const keyword = container.querySelector(".cm-shiki-token");
    expect(keyword).toHaveTextContent("const");
    expect(keyword?.getAttribute("style")).toMatch(/^color: ?(#d73a49|rgb\(215, 58, 73\))/i);
    expect(keyword?.getAttribute("style")).toMatch(/--shiki-dark: ?#f97583/i);
  });

  it("refreshes in place when the file changes", () => {
    const { container, rerender } = renderViewer();
    rerender({ content: `${SOURCE}\n// added` });
    expect(lines(container).at(-1)).toBe("// added");
  });

  it("shows another file's own text when it opens with the content the last file was refreshed to", () => {
    const { container, rerender } = renderViewer({ path: "src/a.ts", content: "first" });
    rerender({ path: "src/a.ts", content: "second" });
    rerender({ path: "src/b.ts", content: "second" });
    expect(lines(container)).toEqual(["second"]);
  });

  it("follows the wrap lines preference", () => {
    const { container, rerender } = renderViewer();
    expect(container.querySelector(".cm-lineWrapping")).toBeNull();
    rerender({ wrapLines: true });
    expect(container.querySelector(".cm-lineWrapping")).not.toBeNull();
  });

  it("highlights a cited range and clears it with the position", () => {
    const { container, rerender } = renderViewer({ position: { line: 2, endLine: 3 } });
    expect([...container.querySelectorAll(".cm-cited-line")].map((l) => l.textContent)).toEqual([
      "const beta = alpha;",
      "export { alpha, beta };",
    ]);
    rerender({ position: undefined });
    expect(container.querySelectorAll(".cm-cited-line")).toHaveLength(0);
  });

  it("clamps a citation past the end of the file to the last line", () => {
    const { container } = renderViewer({ position: { line: 99 } });
    expect(container.querySelector(".cm-cited-line")).toHaveTextContent("export { alpha, beta };");
  });

  it("shows the truncated and large Markdown banners", () => {
    renderViewer({ truncated: true, largeMarkdown: true, path: "big.md" });
    expect(screen.getByText(/truncated/i)).toBeInTheDocument();
    expect(screen.getByText(/Large Markdown file opened as source/)).toBeInTheDocument();
  });

  it("owns ⌘↵, so a pending prompt's accept leaves the editor be", () => {
    const { container } = renderViewer();
    const content = container.querySelector(".cm-content")!;
    expect(content.closest(`[${OWNS_COMMAND_ENTER_ATTRIBUTE}]`)).not.toBeNull();
  });

  it("saves its scroll offset under its own key", () => {
    const { container } = renderViewer();
    const scroller = container.querySelector<HTMLElement>(".cm-scroller")!;
    scroller.scrollTop = 120;
    fireEvent.scroll(scroller);
    expect(getSavedScrollTop("codemirror:conv_1:src/values.ts")).toBe(120);
  });
});

describe("CodeMirrorFileViewer find", () => {
  it("opens the find bar, counts matches, and steps through them", async () => {
    const { rerender } = renderViewer();
    rerender({ searchOpen: true, searchFocusNonce: 1 });
    const input = await screen.findByRole("textbox", { name: "Find in file" });
    await waitFor(() => expect(input).toHaveFocus());
    fireEvent.change(input, { target: { value: "alpha" } });
    const status = screen.getByTestId("file-find-status");
    await waitFor(() => expect(status).toHaveTextContent("1 of 3"));
    fireEvent.keyDown(input, { key: "Enter" });
    expect(status).toHaveTextContent("2 of 3");
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(status).toHaveTextContent("1 of 3");
    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    expect(status).toHaveTextContent("2 of 3");
  });

  it("applies the case, whole-word, and regular-expression toggles", async () => {
    const { rerender } = renderViewer({ content: "Alpha alpha alphabet" });
    rerender({ content: "Alpha alpha alphabet", searchOpen: true, searchFocusNonce: 1 });
    const input = await screen.findByRole("textbox", { name: "Find in file" });
    fireEvent.change(input, { target: { value: "alpha" } });
    const status = screen.getByTestId("file-find-status");
    await waitFor(() => expect(status).toHaveTextContent("of 3"));
    const matchCase = screen.getByRole("button", { name: "Match case" });
    fireEvent.click(matchCase);
    expect(matchCase).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(status).toHaveTextContent("of 2"));
    fireEvent.click(screen.getByRole("button", { name: "Match whole word" }));
    await waitFor(() => expect(status).toHaveTextContent("1 of 1"));
    fireEvent.click(screen.getByRole("button", { name: "Use regular expression" }));
    fireEvent.change(input, { target: { value: "(" } });
    await waitFor(() => expect(status).toHaveTextContent("Invalid pattern"));
  });

  it("selects the first match of a new query even when it is empty at the top", async () => {
    const { container, rerender } = renderViewer({ content: "alpha\nbeta\ngamma" });
    rerender({ content: "alpha\nbeta\ngamma", searchOpen: true, searchFocusNonce: 1 });
    const input = await screen.findByRole("textbox", { name: "Find in file" });
    fireEvent.click(screen.getByRole("button", { name: "Use regular expression" }));
    fireEvent.change(input, { target: { value: "^" } });
    const status = screen.getByTestId("file-find-status");
    const view = EditorView.findFromDOM(container.querySelector<HTMLElement>(".cm-editor")!)!;
    await waitFor(() => expect(status).toHaveTextContent("1 of 3"));
    expect(view.state.selection.main.from).toBe(0);
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(status).toHaveTextContent("2 of 3"));
    expect(view.state.selection.main.from).toBe(6);
  });

  it("closes with Escape and reports it to the toolbar", async () => {
    const onSearchHandled = vi.fn();
    const { rerender } = renderViewer({ onSearchHandled });
    rerender({ onSearchHandled, searchOpen: true, searchFocusNonce: 1 });
    const input = await screen.findByRole("textbox", { name: "Find in file" });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onSearchHandled).toHaveBeenCalled();
    rerender({ onSearchHandled, searchOpen: false, searchFocusNonce: 1 });
    expect(screen.queryByRole("textbox", { name: "Find in file" })).toBeNull();
  });

  it("seeds the query from the selected text", async () => {
    const { container, rerender } = renderViewer();
    const content = container.querySelector<HTMLElement>(".cm-content")!;
    // Select "beta" on line 2 the way a mouse drag would.
    act(() => {
      EditorView.findFromDOM(content)!.dispatch({ selection: { anchor: 23, head: 27 } });
    });
    rerender({ searchOpen: true, searchFocusNonce: 1 });
    const input = await screen.findByRole("textbox", { name: "Find in file" });
    expect(input).toHaveValue("beta");
    expect(screen.getByTestId("file-find-status")).toHaveTextContent("1 of 2");
  });

  it("seeds a multiline query from a selection across CRLF breaks", async () => {
    const { container, rerender } = renderViewer({ content: CRLF_PAIRS });
    act(() => {
      EditorView.findFromDOM(container.querySelector<HTMLElement>(".cm-content")!)!.dispatch({
        selection: { anchor: 0, head: 10 },
      });
    });
    rerender({ searchOpen: true, searchFocusNonce: 1 });
    const input = await screen.findByRole("textbox", { name: "Find in file" });
    expect(input).toHaveValue("alpha\\nbeta");
    await waitFor(() => expect(screen.getByTestId("file-find-status")).toHaveTextContent("1 of 2"));
  });

  it("doesn't report a close when the viewer unmounts with find open", async () => {
    const onSearchHandled = vi.fn();
    const { rerender, unmount } = renderViewer({ onSearchHandled });
    rerender({ onSearchHandled, searchOpen: true, searchFocusNonce: 1 });
    await screen.findByRole("textbox", { name: "Find in file" });
    unmount();
    expect(onSearchHandled).not.toHaveBeenCalled();
  });
});

// Owns the find toggle the way CodeViewer does.
function ViewerWithFindToggle({ content = SOURCE }: { content?: string }) {
  const [searchOpen, setSearchOpen] = useState(false);
  return (
    <TooltipProvider>
      <span data-testid="toggle-state">{String(searchOpen)}</span>
      <CodeMirrorFileViewer
        {...viewerProps({ content })}
        searchOpen={searchOpen}
        onSearchHandled={() => setSearchOpen(false)}
        onSearchOpened={() => setSearchOpen(true)}
      />
    </TooltipProvider>
  );
}

describe("CodeMirrorFileViewer find opened by the editor", () => {
  // Control+G is Go to line on every platform, so only macOS steps with ⌘G.
  const findChords: [string, KeyboardEventInit][] = [["F3", { key: "F3" }]];
  if (/Mac/.test(navigator.platform)) findChords.push(["⌘G", { key: "g", metaKey: true }]);
  it.each(findChords)(
    "keeps the toolbar toggle in sync when %s opens find",
    async (_label, chord) => {
      const { container } = render(<ViewerWithFindToggle />);
      const content = container.querySelector<HTMLElement>(".cm-content")!;
      content.focus();
      fireEvent.keyDown(content, chord);
      await screen.findByRole("textbox", { name: "Find in file" });
      expect(screen.getByTestId("toggle-state")).toHaveTextContent("true");
      fireEvent.click(screen.getByRole("button", { name: "Close find" }));
      await waitFor(() =>
        expect(screen.queryByRole("textbox", { name: "Find in file" })).toBeNull(),
      );
      expect(screen.getByTestId("toggle-state")).toHaveTextContent("false");
    },
  );

  it.each([
    [
      "Escape in the query",
      () => fireEvent.keyDown(screen.getByTestId("file-find-input"), { key: "Escape" }),
    ],
    ["Close find", () => fireEvent.click(screen.getByRole("button", { name: "Close find" }))],
  ])("closes the panel with %s even when the toolbar never opened it", async (_label, close) => {
    const onSearchHandled = vi.fn();
    const { container } = renderViewer({ onSearchHandled });
    const content = container.querySelector<HTMLElement>(".cm-content")!;
    content.focus();
    fireEvent.keyDown(content, { key: "F3" });
    await screen.findByRole("textbox", { name: "Find in file" });
    close();
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Find in file" })).toBeNull());
    expect(onSearchHandled).toHaveBeenCalled();
  });

  it("seeds a multiline query across CRLF breaks when F3 opens find", async () => {
    const { container } = render(<ViewerWithFindToggle content={CRLF_PAIRS} />);
    const content = container.querySelector<HTMLElement>(".cm-content")!;
    content.focus();
    act(() => EditorView.findFromDOM(content)!.dispatch({ selection: { anchor: 0, head: 10 } }));
    fireEvent.keyDown(content, { key: "F3" });
    const input = await screen.findByRole("textbox", { name: "Find in file" });
    expect(input).toHaveValue("alpha\\nbeta");
    await waitFor(() => expect(screen.getByTestId("file-find-status")).toHaveTextContent("1 of 2"));
  });

  it("steps to the next match with F3 once find is open", async () => {
    const { container, rerender } = renderViewer();
    rerender({ searchOpen: true, searchFocusNonce: 1 });
    const input = await screen.findByRole("textbox", { name: "Find in file" });
    fireEvent.change(input, { target: { value: "alpha" } });
    const status = screen.getByTestId("file-find-status");
    await waitFor(() => expect(status).toHaveTextContent("1 of 3"));
    fireEvent.keyDown(container.querySelector<HTMLElement>(".cm-content")!, { key: "F3" });
    await waitFor(() => expect(status).toHaveTextContent("2 of 3"));
  });
});

describe("CodeMirrorFileViewer comments", () => {
  const CRLF_NOTES = "# Notes\r\nFirst point.\r\nSecond point.\r\n";
  const secondPoint = CRLF_NOTES.indexOf("Second");
  const savedComment = {
    id: "c_1",
    conversation_id: "conv_1",
    path: "notes.md",
    start_index: secondPoint,
    end_index: secondPoint + "Second".length,
    body: "Expand this",
    status: "draft" as const,
    created_at: 0,
    updated_at: 0,
    anchor_content: "Second",
  } as Comment;

  function editorView(container: HTMLElement): EditorView {
    return EditorView.findFromDOM(container.querySelector(".cm-editor") as HTMLElement)!;
  }

  function commentProps(
    overrides: Partial<NonNullable<CodeMirrorFileViewerProps["commentOptions"]>> = {},
  ) {
    return {
      path: "notes.md",
      content: CRLF_NOTES,
      commentOptions: {
        comments: [savedComment],
        activeSelection: null,
        onSetActiveSelection: vi.fn(),
        canComment: true,
        ...overrides,
      },
    };
  }

  it("highlights a saved comment at its raw offsets in a CRLF file", () => {
    const { container } = renderViewer(commentProps());
    expect(container.querySelector(".cm-comment")).toHaveTextContent(/^Second$/);
  });

  it("opens a comment when the reader clicks inside it", () => {
    const onSetActiveSelection = vi.fn();
    const { container } = renderViewer(commentProps({ onSetActiveSelection }));
    const view = editorView(container);
    const inside = view.state.doc.line(3).from + 2;
    vi.spyOn(view, "posAtCoords").mockReturnValue(inside);
    act(() => view.dispatch({ selection: { anchor: inside } }));
    fireEvent.mouseUp(view.contentDOM);
    expect(onSetActiveSelection).toHaveBeenCalledWith({
      start_index: savedComment.start_index,
      end_index: savedComment.end_index,
      anchor_content: "Second",
    });
  });

  it("clears the active comment on a click elsewhere unless a draft is in progress", () => {
    const onSetActiveSelection = vi.fn();
    const pendingBodyRef = { current: "" };
    const activeSelection = { start_index: 0, end_index: 1, anchor_content: "#" };
    const { container } = renderViewer(
      commentProps({ onSetActiveSelection, activeSelection, pendingBodyRef }),
    );
    const view = editorView(container);
    vi.spyOn(view, "posAtCoords").mockReturnValue(view.state.doc.line(2).from);
    pendingBodyRef.current = "Half-written";
    fireEvent.mouseUp(view.contentDOM);
    expect(onSetActiveSelection).not.toHaveBeenCalled();
    pendingBodyRef.current = "";
    fireEvent.mouseUp(view.contentDOM);
    expect(onSetActiveSelection).toHaveBeenCalledWith(null);
  });

  it("adds a comment on a selection across CRLF lines with raw offsets", async () => {
    const onSetActiveSelection = vi.fn();
    const { container } = renderViewer(commentProps({ onSetActiveSelection }));
    const view = editorView(container);
    vi.spyOn(view, "coordsAtPos").mockReturnValue({ left: 40, right: 41, top: 80, bottom: 96 });
    act(() => {
      view.focus();
      view.dispatch({
        selection: {
          anchor: view.state.doc.line(2).from + 6,
          head: view.state.doc.line(3).from + 6,
        },
      });
    });
    const addComment = await screen.findByRole("button", { name: "Add comment" });
    fireEvent.mouseDown(addComment);
    fireEvent.click(addComment);
    const anchor = "point.\r\nSecond";
    const start = CRLF_NOTES.indexOf(anchor);
    expect(onSetActiveSelection).toHaveBeenCalledWith({
      start_index: start,
      end_index: start + anchor.length,
      anchor_content: anchor,
    });
    expect(screen.queryByRole("button", { name: "Add comment" })).toBeNull();
  });

  it("offers no Add comment button where the file can't take comments", async () => {
    const { container } = renderViewer(commentProps({ canComment: false }));
    const view = editorView(container);
    vi.spyOn(view, "coordsAtPos").mockReturnValue({ left: 40, right: 41, top: 80, bottom: 96 });
    act(() => {
      view.focus();
      view.dispatch({ selection: { anchor: 0, head: 7 } });
    });
    await act(async () => {
      await new Promise<void>((resolve) => {
        requestAnimationFrame(() => resolve());
      });
    });
    expect(screen.queryByRole("button", { name: "Add comment" })).toBeNull();
    // Saved comments still show.
    expect(container.querySelector(".cm-comment")).not.toBeNull();
  });

  it("copies with the file's CRLF breaks and follows a refresh that changes only line breaks", () => {
    const { container, rerender } = renderViewer(commentProps());
    const view = editorView(container);
    const copied = () => {
      const setData = vi.fn();
      const event = new Event("copy", { bubbles: true, cancelable: true });
      Object.defineProperty(event, "clipboardData", { value: { setData } });
      view.contentDOM.dispatchEvent(event);
      return setData.mock.calls.at(-1)?.[1];
    };
    act(() => view.dispatch({ selection: { anchor: 0, head: view.state.doc.line(2).to } }));
    expect(copied()).toBe("# Notes\r\nFirst point.");

    // Saved again with LF breaks: the same comment now starts two characters earlier.
    const lfNotes = CRLF_NOTES.replace(/\r\n/g, "\n");
    const lfStart = lfNotes.indexOf("Second");
    rerender({
      content: lfNotes,
      commentOptions: {
        ...commentProps().commentOptions,
        comments: [{ ...savedComment, start_index: lfStart, end_index: lfStart + 6 }],
      },
    });
    expect(container.querySelector(".cm-comment")).toHaveTextContent(/^Second$/);
    act(() => view.dispatch({ selection: { anchor: 0, head: view.state.doc.line(2).to } }));
    expect(copied()).toBe("# Notes\nFirst point.");
  });
});

describe("CodeMirrorFileViewer selection actions from the keyboard", () => {
  const NOTES = "# Notes\r\nFirst point.\r\nSecond point.\r\nThird point.\r\n";
  let attachments: ReturnType<typeof vi.fn<(attachment: unknown) => void>>;
  beforeEach(() => {
    attachments = vi.fn<(attachment: unknown) => void>();
    useChatStore.setState({ sessionHarness: null, addComposerAttachment: attachments });
  });
  afterEach(() => useChatStore.setState({ sessionHarness: null }));

  function renderNotes(
    overrides: Partial<NonNullable<CodeMirrorFileViewerProps["commentOptions"]>> = {},
  ) {
    const onSetActiveSelection = vi.fn();
    const result = renderViewer({
      path: "notes.md",
      content: NOTES,
      commentOptions: {
        comments: [],
        activeSelection: null,
        onSetActiveSelection,
        canComment: true,
        ...overrides,
      },
    });
    const view = EditorView.findFromDOM(
      result.container.querySelector(".cm-editor") as HTMLElement,
    )!;
    vi.spyOn(view, "coordsAtPos").mockReturnValue({ left: 40, right: 41, top: 80, bottom: 96 });
    // Lines two and three, ending at the start of line four.
    const select = () =>
      act(() => {
        view.focus();
        view.dispatch({
          selection: { anchor: view.state.doc.line(2).from, head: view.state.doc.line(4).from },
        });
      });
    return { ...result, view, select, onSetActiveSelection };
  }

  function selectionCommands() {
    return Object.values(usePaletteCommandsStore.getState().groups)
      .filter((group) => group.heading === "Selection")
      .flatMap((group) => group.commands);
  }

  it("keeps the toolbar when focus moves onto it, and Escape returns to the selection", async () => {
    const { view, select, onSetActiveSelection } = renderNotes();
    select();
    const addComment = await screen.findByRole("button", { name: "Add comment" });
    act(() => addComment.focus());
    // The editor reports losing focus a moment after it happens.
    await act(async () => {
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 50);
      });
    });
    expect(view.hasFocus).toBe(false);
    expect(screen.getByRole("toolbar", { name: "Selection actions" })).toBeInTheDocument();

    fireEvent.keyDown(addComment, { key: "Escape" });
    expect(view.hasFocus).toBe(true);
    expect(view.state.selection.main.to).toBe(view.state.doc.line(4).from);

    const again = await screen.findByRole("button", { name: "Add comment" });
    act(() => again.focus());
    // A keyboard press on a focused button is a click.
    fireEvent.click(again);
    const start = NOTES.indexOf("First");
    expect(onSetActiveSelection).toHaveBeenCalledWith({
      start_index: start,
      end_index: NOTES.indexOf("Third"),
      anchor_content: "First point.\r\nSecond point.\r\n",
    });
  });

  it("hides the toolbar when focus leaves for something else", async () => {
    const { select } = renderNotes();
    select();
    const addComment = await screen.findByRole("button", { name: "Add comment" });
    act(() => addComment.focus());
    const elsewhere = document.createElement("button");
    document.body.append(elsewhere);
    act(() => elsewhere.focus());
    await waitFor(() => expect(screen.queryByRole("toolbar")).toBeNull());
    elsewhere.remove();
  });

  it("offers Add comment in the command palette while there is a selection", () => {
    const { view, select, onSetActiveSelection } = renderNotes();
    expect(selectionCommands()).toEqual([]);
    select();
    // The palette takes focus; the selection stays.
    act(() => view.contentDOM.blur());
    const commands = selectionCommands();
    expect(commands.map((command) => command.label)).toEqual(["Add comment"]);
    act(() => commands[0]!.run());
    expect(onSetActiveSelection).toHaveBeenCalledWith(
      expect.objectContaining({ anchor_content: "First point.\r\nSecond point.\r\n" }),
    );
  });

  it("attaches the selected lines to a native coding agent from the palette and the toolbar", async () => {
    useChatStore.setState({ sessionHarness: "codex-native" });
    const { select } = renderNotes();
    select();
    const attach = selectionCommands().find((command) => command.label === "Attach to agent")!;
    act(() => attach.run());
    // The selection ends at the start of line four, which it doesn't include.
    expect(attachments).toHaveBeenLastCalledWith({
      path: "notes.md",
      isDir: false,
      lineRange: { start: 2, end: 3 },
    });

    select();
    const button = await screen.findByRole("button", { name: "Attach to agent" });
    act(() => button.focus());
    fireEvent.click(button);
    expect(attachments).toHaveBeenCalledTimes(2);
  });

  it("offers nothing where the file can't take comments or the viewer is hidden", () => {
    useChatStore.setState({ sessionHarness: "codex-native" });
    const readOnly = renderNotes({ canComment: false });
    readOnly.select();
    expect(selectionCommands()).toEqual([]);
    readOnly.unmount();

    const hidden = renderNotes({ onScreen: false });
    hidden.select();
    expect(selectionCommands()).toEqual([]);
  });
});

describe("CodeMirrorFileViewer revealing the active comment", () => {
  const LONG = Array.from({ length: 80 }, (_unused, index) => `line ${index + 1}`).join("\n");
  const lineFifty = LONG.indexOf("line 50");
  const comment = { start_index: lineFifty, end_index: lineFifty + 7, anchor_content: "line 50" };

  function scrollRequests(dispatch: ReturnType<typeof vi.spyOn>) {
    return dispatch.mock.calls.filter(([spec]: [{ effects?: unknown }]) => {
      const effects = [spec?.effects ?? []].flat() as { is?: (type: unknown) => boolean }[];
      return effects.length > 0 && !effects.some((effect) => effect.is?.(setCommentLayer));
    });
  }

  function renderLong(lineTop: number) {
    const props = (activeSelection: typeof comment | null) => ({
      content: LONG,
      commentOptions: {
        comments: [],
        activeSelection,
        onSetActiveSelection: () => {},
        canComment: true,
      },
    });
    const result = renderViewer(props(null));
    const view = EditorView.findFromDOM(
      result.container.querySelector(".cm-editor") as HTMLElement,
    )!;
    // A 200px screen; the line is rendered, at `lineTop`.
    vi.spyOn(view.scrollDOM, "getBoundingClientRect").mockReturnValue({
      ...EMPTY_RECT,
      top: 8,
      bottom: 208,
      left: 0,
      right: 600,
    } as DOMRect);
    vi.spyOn(view, "coordsAtPos").mockReturnValue({
      left: 60,
      right: 61,
      top: lineTop,
      bottom: lineTop + 16,
    });
    const dispatch = vi.spyOn(view, "dispatch");
    result.rerender(props(comment));
    return scrollRequests(dispatch);
  }

  it("scrolls to a comment that is rendered just below the screen", () => {
    expect(renderLong(594)).toHaveLength(1);
  });

  it("leaves the view alone when the comment is on screen", () => {
    expect(renderLong(100)).toHaveLength(0);
  });
});

// The platform's command key, as CodeMirror reads it.
const MOD = /Mac/.test(navigator.platform) ? { metaKey: true } : { ctrlKey: true };

function editing(
  editable = true,
  readOnlyReason?: string,
): NonNullable<CodeMirrorFileViewerProps["editing"]> {
  return {
    editable,
    readOnlyReason,
    editorRef: { current: null },
    onChange: () => {},
    onSave: () => {},
    onBlur: () => {},
  };
}

function editorViewOf(container: HTMLElement): EditorView {
  return EditorView.findFromDOM(container.querySelector<HTMLElement>(".cm-editor")!)!;
}

/** The File group's commands the palette lists, runnable or greyed out. */
function fileCommands() {
  return Object.values(usePaletteCommandsStore.getState().groups)
    .filter((group) => group.heading === "File")
    .flatMap((group) => group.commands)
    .filter((command) => isCommandListed(command.availability));
}

describe("CodeMirrorFileViewer find and replace", () => {
  async function openFind(overrides: Partial<CodeMirrorFileViewerProps> = {}) {
    const rendered = renderViewer({ editing: editing(), ...overrides });
    rendered.rerender({ searchOpen: true, searchFocusNonce: 1 });
    const input = await screen.findByRole("textbox", { name: "Find in file" });
    return { ...rendered, input, view: editorViewOf(rendered.container) };
  }

  it("replaces one match at a time, then every match as one undoable edit", async () => {
    const { input, view } = await openFind();
    fireEvent.change(input, { target: { value: "alpha" } });
    const status = screen.getByTestId("file-find-status");
    await waitFor(() => expect(status).toHaveTextContent("1 of 3"));

    const toggle = screen.getByRole("button", { name: "Replace" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    const replacement = screen.getByRole("textbox", { name: "Replace with" });
    fireEvent.change(replacement, { target: { value: "omega" } });
    // Typing a replacement doesn't search again.
    expect(status).toHaveTextContent("1 of 3");

    fireEvent.click(screen.getByRole("button", { name: "Replace match" }));
    expect(view.state.doc.line(1).text).toBe("const omega = 1;");
    await waitFor(() => expect(status).toHaveTextContent("1 of 2"));
    expect(view.state.sliceDoc(view.state.selection.main.from, view.state.selection.main.to)).toBe(
      "alpha",
    );

    fireEvent.keyDown(replacement, { key: "Enter", ...MOD });
    expect(view.state.doc.toString()).not.toContain("alpha");
    await waitFor(() => expect(status).toHaveTextContent("No results"));
    const { undo } = await import("@codemirror/commands");
    act(() => {
      undo(view);
    });
    expect(view.state.doc.toString().match(/alpha/g)).toHaveLength(2);
  });

  it("replaces with the case, whole-word, and regular-expression options", async () => {
    const content = "Alpha alpha alphabet\nlet count = 1;";
    const { input, view } = await openFind({ content });
    fireEvent.click(screen.getByRole("button", { name: "Replace" }));
    const replacement = screen.getByRole("textbox", { name: "Replace with" });
    const status = screen.getByTestId("file-find-status");

    fireEvent.click(screen.getByRole("button", { name: "Match case" }));
    fireEvent.click(screen.getByRole("button", { name: "Match whole word" }));
    fireEvent.change(input, { target: { value: "alpha" } });
    await waitFor(() => expect(status).toHaveTextContent("1 of 1"));
    fireEvent.change(replacement, { target: { value: "beta" } });
    fireEvent.click(screen.getByRole("button", { name: "Replace all" }));
    expect(view.state.doc.line(1).text).toBe("Alpha beta alphabet");

    fireEvent.click(screen.getByRole("button", { name: "Match whole word" }));
    fireEvent.click(screen.getByRole("button", { name: "Use regular expression" }));
    fireEvent.change(input, { target: { value: "(\\w+) = (\\d)" } });
    await waitFor(() => expect(status).toHaveTextContent("1 of 1"));
    fireEvent.change(replacement, { target: { value: "$2 = $1" } });
    fireEvent.keyDown(replacement, { key: "Enter" });
    expect(view.state.doc.line(2).text).toBe("let 1 = count;");
  });

  it.each([
    ["^", ">", ">alpha\n>beta\n>gamma"],
    ["$", "<", "alpha<\nbeta<\ngamma<"],
  ])("replaces the empty match %j on each line in turn, then wraps", async (query, insert, all) => {
    const { input, view } = await openFind({ content: "alpha\nbeta\ngamma" });
    fireEvent.click(screen.getByRole("button", { name: "Use regular expression" }));
    fireEvent.change(input, { target: { value: query } });
    const status = screen.getByTestId("file-find-status");
    await waitFor(() => expect(status).toHaveTextContent("1 of 3"));
    fireEvent.click(screen.getByRole("button", { name: "Replace" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Replace with" }), {
      target: { value: insert },
    });
    // Each replacement waits for the index to catch up before the next.
    const replaceAndExpect = async (ordinal: number) => {
      fireEvent.click(screen.getByRole("button", { name: "Replace match" }));
      await waitFor(() => expect(status).toHaveTextContent(`${ordinal} of 3`));
    };
    await replaceAndExpect(2);
    await replaceAndExpect(3);
    await replaceAndExpect(1);
    expect(view.state.doc.toString()).toBe(all);
    // Wrapped to the first line's match, which isn't replaced again until asked.
    expect(view.state.doc.lineAt(view.state.selection.main.head).number).toBe(1);
  });

  it("moves to the next match when a replacement changes nothing", async () => {
    const { input, view } = await openFind({ content: "alpha\nbeta\ngamma" });
    fireEvent.click(screen.getByRole("button", { name: "Use regular expression" }));
    fireEvent.change(input, { target: { value: "^" } });
    const status = screen.getByTestId("file-find-status");
    await waitFor(() => expect(status).toHaveTextContent("1 of 3"));
    fireEvent.click(screen.getByRole("button", { name: "Replace" }));
    fireEvent.click(screen.getByRole("button", { name: "Replace match" }));
    await waitFor(() => expect(status).toHaveTextContent("2 of 3"));
    expect(view.state.doc.toString()).toBe("alpha\nbeta\ngamma");
  });

  it("replaces with lookaround context and skips matches inside a character", async () => {
    const { input, view } = await openFind({ content: "foobar café e" });
    fireEvent.click(screen.getByRole("button", { name: "Replace" }));
    const replacement = screen.getByRole("textbox", { name: "Replace with" });
    const status = screen.getByTestId("file-find-status");

    fireEvent.click(screen.getByRole("button", { name: "Use regular expression" }));
    fireEvent.change(input, { target: { value: "(foo)(?=bar)" } });
    await waitFor(() => expect(status).toHaveTextContent("1 of 1"));
    fireEvent.change(replacement, { target: { value: "$1!" } });
    fireEvent.click(screen.getByRole("button", { name: "Replace all" }));
    await waitFor(() => expect(view.state.doc.toString()).toBe("foo!bar café e"));

    fireEvent.click(screen.getByRole("button", { name: "Use regular expression" }));
    fireEvent.change(input, { target: { value: "e" } });
    await waitFor(() => expect(status).toHaveTextContent("of 2"));
    fireEvent.change(replacement, { target: { value: "X" } });
    fireEvent.click(screen.getByRole("button", { name: "Replace all" }));
    await waitFor(() => expect(view.state.doc.toString()).toBe("foo!bar café X"));
  });

  it("leaves groups that didn't take part empty when replacing", async () => {
    const { input, view } = await openFind({ content: "beta alphabeta" });
    fireEvent.click(screen.getByRole("button", { name: "Use regular expression" }));
    fireEvent.change(input, { target: { value: "(alpha)?beta" } });
    await waitFor(() => expect(screen.getByTestId("file-find-status")).toHaveTextContent("of 2"));
    fireEvent.click(screen.getByRole("button", { name: "Replace" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Replace with" }), {
      target: { value: "$1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Replace all" }));
    await waitFor(() => expect(view.state.doc.toString()).toBe(" alpha"));
  });

  it("offers no replace in a read-only file, where ⌥⇧⌘F opens plain find", async () => {
    const { container } = renderViewer({ editing: editing(false) });
    const content = container.querySelector<HTMLElement>(".cm-content")!;
    content.focus();
    fireEvent.keyDown(content, {
      key: "F",
      code: "KeyF",
      keyCode: 70,
      altKey: true,
      shiftKey: true,
      ...MOD,
    });
    const input = await screen.findByRole("textbox", { name: "Find in file" });
    await waitFor(() => expect(input).toHaveFocus());
    expect(screen.queryByRole("button", { name: "Replace" })).toBeNull();
    expect(fileCommands().map((command) => command.label)).toEqual([
      "Go to line…",
      ...FILE_EDITOR_COMMANDS.filter((command) => !command.edits).map(fileEditorCommandLabel),
    ]);
  });

  it("greys out edit commands with the reason a file can't be edited", () => {
    const reason = "Only part of this large file is loaded, so it can't be edited here.";
    const { container } = renderViewer({ editing: editing(false, reason) });
    const commands = fileCommands();
    const edits = new Set([
      "file.find-and-replace",
      ...FILE_EDITOR_COMMANDS.filter((command) => command.edits).map((command) => command.id),
    ]);
    for (const command of commands) {
      expect(command.availability, command.id).toEqual(
        edits.has(command.id) ? { available: false, reason } : undefined,
      );
    }
    expect(commands.map((command) => command.id)).toEqual(
      expect.arrayContaining([...edits, "file.foldAll"]),
    );
    // Running one anyway, as a stale palette row might, leaves the file alone.
    const view = editorViewOf(container);
    const before = view.state.doc.toString();
    act(() => commands.find((command) => command.id === "file.deleteLines")!.run());
    expect(view.state.doc.toString()).toBe(before);
  });

  it("opens with the replace field focused from ⌥⇧⌘F and from the command palette", async () => {
    render(<EditableViewerWithFindToggle />);
    const content = document.querySelector<HTMLElement>(".cm-content")!;
    content.focus();
    fireEvent.keyDown(content, {
      key: "F",
      code: "KeyF",
      keyCode: 70,
      altKey: true,
      shiftKey: true,
      ...MOD,
    });
    const replacement = await screen.findByRole("textbox", { name: "Replace with" });
    await waitFor(() => expect(replacement).toHaveFocus());
    expect(screen.getByTestId("toggle-state")).toHaveTextContent("true");

    // Already open: the command moves focus back to the replace field.
    act(() => screen.getByRole("textbox", { name: "Find in file" }).focus());
    const command = fileCommands().find((entry) => entry.label === "Find and replace in file")!;
    act(() => command.run());
    await waitFor(() => expect(replacement).toHaveFocus());
  });

  it("leaves ⌥⌘F to the app: it opens neither find nor replace in a focused editor", async () => {
    render(<EditableViewerWithFindToggle />);
    const content = document.querySelector<HTMLElement>(".cm-content")!;
    content.focus();
    fireEvent.keyDown(content, { key: "ƒ", code: "KeyF", keyCode: 70, altKey: true, ...MOD });
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(screen.queryByRole("textbox", { name: "Replace with" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Find in file" })).toBeNull();
    expect(screen.getByTestId("toggle-state")).toHaveTextContent("false");
  });
});

function EditableViewerWithFindToggle({ wrapLines = false }: { wrapLines?: boolean }) {
  const [searchOpen, setSearchOpen] = useState(false);
  const [editingProps] = useState(() => editing());
  return (
    <TooltipProvider>
      <span data-testid="toggle-state">{String(searchOpen)}</span>
      <CodeMirrorFileViewer
        {...viewerProps({ editing: editingProps, wrapLines })}
        searchOpen={searchOpen}
        onSearchHandled={() => setSearchOpen(false)}
        onSearchOpened={() => setSearchOpen(true)}
      />
    </TooltipProvider>
  );
}

describe("CodeMirrorFileViewer wrapped-height rebuild", () => {
  let resizeCallbacks: (() => void)[] = [];
  let frames: FrameRequestCallback[] = [];
  let width = 0;
  beforeEach(() => {
    resizeCallbacks = [];
    frames = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: ResizeObserverCallback) {
          resizeCallbacks.push(() => callback([], this as unknown as ResizeObserver));
        }
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) =>
      frames.push(callback),
    );
    vi.stubGlobal("cancelAnimationFrame", (id: number) => {
      frames[id - 1] = () => {};
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      () => ({ ...EMPTY_RECT, width, height: 100 }) as DOMRect,
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });
  /** The pane settles at `next` and enough animation frames pass to count it settled. */
  const settleAt = (next: number) =>
    act(() => {
      width = next;
      resizeCallbacks.forEach((callback) => callback());
      for (let index = 0; index < 8; index++) {
        const pending = frames;
        frames = [];
        pending.forEach((frame) => frame(0));
      }
    });

  it("keeps an open Find and Replace as it was, and rebuilds once it closes", async () => {
    render(<EditableViewerWithFindToggle wrapLines />);
    const content = document.querySelector<HTMLElement>(".cm-content")!;
    const view = EditorView.findFromDOM(content)!;
    const setState = vi.spyOn(view, "setState");
    content.focus();
    fireEvent.keyDown(content, {
      key: "F",
      code: "KeyF",
      keyCode: 70,
      altKey: true,
      shiftKey: true,
      ...MOD,
    });
    const query = await screen.findByRole("textbox", { name: "Find in file" });
    const replacement = screen.getByRole<HTMLInputElement>("textbox", { name: "Replace with" });
    fireEvent.change(query, { target: { value: "alpha" } });
    fireEvent.change(replacement, { target: { value: "omega" } });
    replacement.focus();
    replacement.setSelectionRange(2, 2);

    settleAt(385.9);

    expect(setState).not.toHaveBeenCalled();
    expect(screen.getByTestId("toggle-state")).toHaveTextContent("true");
    expect(screen.getByRole("textbox", { name: "Find in file" })).toBe(query);
    expect(query).toHaveValue("alpha");
    expect(replacement).toHaveValue("omega");
    expect(replacement).toHaveFocus();
    expect(replacement.selectionStart).toBe(2);

    fireEvent.click(screen.getByRole("button", { name: "Close find" }));
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Find in file" })).toBeNull());
    settleAt(385.9);
    expect(setState).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("toggle-state")).toHaveTextContent("false");
  });
});

/** Errors React reports from event handlers, which reach the window instead of the test. */
function collectWindowErrors(): string[] {
  const errors: string[] = [];
  const onError = (event: ErrorEvent) => {
    errors.push(String(event.error ?? event.message));
    event.preventDefault();
  };
  window.addEventListener("error", onError);
  onTestFinished(() => window.removeEventListener("error", onError));
  return errors;
}

describe("CodeMirrorFileViewer go to line", () => {
  const LONG = Array.from({ length: 80 }, (_unused, index) => `line ${index + 1}`).join("\n");

  function openGoToLine(cursorLine = 1) {
    const rendered = renderViewer({ content: LONG });
    const view = editorViewOf(rendered.container);
    act(() => view.dispatch({ selection: { anchor: view.state.doc.line(cursorLine).from } }));
    view.contentDOM.focus();
    fireEvent.keyDown(view.contentDOM, { key: "g", ctrlKey: true });
    return { ...rendered, view, input: screen.getByRole("textbox", { name: "Go to line" }) };
  }

  it("opens with Control+G, jumps to a line and column on Enter, and focuses the text", async () => {
    const { view, input } = openGoToLine();
    await waitFor(() => expect(input).toHaveFocus());
    expect(screen.getByText(/^Current line 1 of 80/)).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "42:3" } });
    expect(screen.getByText("Go to line 42, column 3 of 80.")).toBeInTheDocument();
    fireEvent.submit(input.closest("form")!);
    expect(screen.queryByRole("textbox", { name: "Go to line" })).toBeNull();
    expect(view.state.selection.main.head).toBe(view.state.doc.line(42).from + 2);
    expect(view.contentDOM).toHaveFocus();
  });

  it("keeps a line past the end on the last line, and explains text it can't read", () => {
    const { view, input } = openGoToLine();
    fireEvent.change(input, { target: { value: "line five" } });
    expect(
      screen.getByText("Type a line number from 1 to 80, or line:column."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go" })).toBeDisabled();
    fireEvent.change(input, { target: { value: "500" } });
    fireEvent.submit(input.closest("form")!);
    expect(view.state.doc.lineAt(view.state.selection.main.head).number).toBe(80);
  });

  it("puts the cursor back where it was on Escape", () => {
    const { view, input } = openGoToLine(10);
    expect(screen.getByText(/^Current line 10 of 80/)).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "70" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("textbox", { name: "Go to line" })).toBeNull();
    expect(view.state.doc.lineAt(view.state.selection.main.head).number).toBe(10);
    expect(view.contentDOM).toHaveFocus();
  });

  it("puts the cursor back inside the file on Escape after a refresh shortened it", () => {
    const { view, input, rerender } = openGoToLine(70);
    fireEvent.change(input, { target: { value: "40" } });
    rerender({ content: "short\nfile" });
    expect(view.state.doc.toString()).toBe("short\nfile");
    const errors = collectWindowErrors();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(errors).toEqual([]);
    expect(screen.queryByRole("textbox", { name: "Go to line" })).toBeNull();
    expect(view.state.selection.main.head).toBeLessThanOrEqual(view.state.doc.length);
  });

  it("puts the cursor back inside the file after an editor loads shorter content", () => {
    const editorProps = editing();
    const rendered = renderViewer({ content: LONG, editing: editorProps });
    const view = editorViewOf(rendered.container);
    act(() => view.dispatch({ selection: { anchor: view.state.doc.line(70).from } }));
    view.contentDOM.focus();
    fireEvent.keyDown(view.contentDOM, { key: "g", ctrlKey: true });
    const input = screen.getByRole("textbox", { name: "Go to line" });
    act(() => editorProps.editorRef.current!.loadContent("short\nfile"));
    const errors = collectWindowErrors();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(errors).toEqual([]);
    expect(screen.queryByRole("textbox", { name: "Go to line" })).toBeNull();
    expect(view.state.selection.main.head).toBeLessThanOrEqual(view.state.doc.length);
  });

  it("is a command in the palette while the viewer is on screen", async () => {
    const { unmount } = renderViewer({ content: LONG, editing: editing() });
    const labels = () => fileCommands().map((command) => command.label);
    expect(labels()).toEqual([
      "Go to line…",
      "Find and replace in file",
      ...FILE_EDITOR_COMMANDS.map(fileEditorCommandLabel),
    ]);
    act(() => fileCommands()[0]!.run());
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Go to line" })).toHaveFocus());
    unmount();
    expect(labels()).toEqual([]);

    renderViewer({ commentOptions: { ...NO_COMMENT_OPTIONS, onScreen: false } });
    expect(labels()).toEqual([]);
  });
});

const NO_COMMENT_OPTIONS = {
  comments: [],
  activeSelection: null,
  onSetActiveSelection: () => {},
  canComment: false,
};

describe("CodeMirrorFileViewer multiple cursors", () => {
  it("adds cursors above and below, and the next occurrence of the selection", () => {
    const { container } = renderViewer({ editing: editing() });
    const view = editorViewOf(container);
    view.contentDOM.focus();
    act(() => view.dispatch({ selection: { anchor: 6 } }));
    fireEvent.keyDown(view.contentDOM, { key: "ArrowDown", altKey: true, ...MOD });
    expect(view.state.selection.ranges).toHaveLength(2);

    // "alpha" on line 1, then its next occurrence on line 2.
    act(() => view.dispatch({ selection: { anchor: 6, head: 11 } }));
    fireEvent.keyDown(view.contentDOM, { key: "d", ...MOD });
    expect(
      view.state.selection.ranges.map((range) => view.state.sliceDoc(range.from, range.to)),
    ).toEqual(["alpha", "alpha"]);
    act(() => view.dispatch(view.state.replaceSelection("omega")));
    expect(view.state.doc.toString().match(/omega/g)).toHaveLength(2);
  });

  it("adds a cursor on Alt-click rather than the command key", () => {
    const { container } = renderViewer();
    const addsRange = (init: MouseEventInit) =>
      editorViewOf(container)
        .state.facet(EditorView.clickAddsSelectionRange)
        .some((test) => test(new MouseEvent("mousedown", init)));
    expect(addsRange({ altKey: true })).toBe(true);
    expect(addsRange({ ...MOD })).toBe(false);
  });
});
