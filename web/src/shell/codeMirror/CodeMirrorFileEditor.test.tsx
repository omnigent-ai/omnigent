import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { undo } from "@codemirror/commands";
import { EditorView } from "@codemirror/view";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type * as FileContentModule from "@/hooks/useFileContent";
import { TooltipProvider } from "@/components/ui/tooltip";

vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn().mockReturnValue(true) }));
vi.mock("@/hooks/useWriteFileContent", () => ({ useWriteFileContent: vi.fn() }));
vi.mock("@/hooks/useFileContent", async (importOriginal) => ({
  ...(await importOriginal<typeof FileContentModule>()),
  fetchFileContent: vi.fn(),
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(() => true),
  useSessionHostOnline: vi.fn(() => true),
}));

import { fetchFileContent, type FileContentResponse } from "@/hooks/useFileContent";
import * as permissions from "@/hooks/usePermissions";
import * as writeHook from "@/hooks/useWriteFileContent";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import {
  CodeMirrorFileEditor,
  type CodeMirrorFileEditorProps,
  NO_EDIT_ACCESS_REASON,
  TRUNCATED_READ_ONLY_REASON,
} from "./CodeMirrorFileEditor";
import { reestimateHeights, reestimateWrappedHeightsOnSettledWidth } from "./wrappedHeightEstimate";

// jsdom has no layout; CodeMirror measures ranges when it scrolls.
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

const PATH = "src/values.ts";
const SOURCE = "const alpha = 1;\nconst beta = (alpha);\n";
const CRLF_SOURCE = "const alpha = 1;\r\nconst beta = 2;\r\n";

let mutateAsync: ReturnType<typeof vi.fn>;

function serverFile(content: string): FileContentResponse {
  return {
    object: "session.environment.filesystem.file_content",
    path: PATH,
    content_type: "text/typescript",
    encoding: "utf-8",
    content,
    bytes: content.length,
  };
}

function editorProps(
  overrides: Partial<CodeMirrorFileEditorProps> = {},
): CodeMirrorFileEditorProps {
  return {
    conversationId: "conv_editor",
    path: PATH,
    content: SOURCE,
    truncated: false,
    wrapLines: false,
    searchOpen: false,
    onSearchHandled: () => {},
    onSearchOpened: () => {},
    isSettled: true,
    ...overrides,
  };
}

function renderEditor(overrides: Partial<CodeMirrorFileEditorProps> = {}) {
  const props = editorProps(overrides);
  const result = render(
    <TooltipProvider>
      <CodeMirrorFileEditor {...props} />
    </TooltipProvider>,
  );
  const view = () =>
    EditorView.findFromDOM(result.container.querySelector(".cm-editor") as HTMLElement)!;
  const rerender = (next: Partial<CodeMirrorFileEditorProps>) =>
    result.rerender(
      <TooltipProvider>
        <CodeMirrorFileEditor {...props} {...next} />
      </TooltipProvider>,
    );
  return { ...result, view, rerender };
}

/** Types `text` at `at` the way a keystroke does. */
function type(view: EditorView, at: number, text: string) {
  act(() => {
    view.dispatch({ changes: { from: at, insert: text }, userEvent: "input.type" });
  });
}

async function advance(milliseconds: number) {
  await act(async () => {
    vi.advanceTimersByTime(milliseconds);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  mutateAsync = vi.fn().mockResolvedValue(undefined);
  vi.mocked(writeHook.useWriteFileContent).mockReturnValue({
    isPending: false,
    isError: false,
    isSuccess: false,
    reset: vi.fn(),
    mutateAsync,
  } as unknown as ReturnType<typeof writeHook.useWriteFileContent>);
  vi.mocked(fetchFileContent).mockImplementation(async () => serverFile(SOURCE));
  vi.mocked(permissions.useCanEdit).mockReturnValue(true);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("CodeMirrorFileEditor saving", () => {
  it("saves an edit after a pause", async () => {
    const { view } = renderEditor();
    expect(view().contentDOM).not.toHaveAttribute("aria-readonly", "true");
    type(view(), 0, "// note\n");
    expect(mutateAsync).not.toHaveBeenCalled();
    await advance(1000);
    expect(mutateAsync).toHaveBeenCalledTimes(1);
    expect(mutateAsync).toHaveBeenCalledWith({ path: PATH, content: `// note\n${SOURCE}` });
  });

  it("keeps an unsaved edit, its save and its undo through a wrapped-height rebuild", async () => {
    const onDirtyChange = vi.fn();
    const { view } = renderEditor({ wrapLines: true, onDirtyChange });
    expect(view().plugin(reestimateWrappedHeightsOnSettledWidth)).not.toBeNull();
    type(view(), 0, "// note\n");
    act(() => reestimateHeights(view()));

    expect(view().state.doc.toString()).toBe(`// note\n${SOURCE}`);
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    await advance(1000);
    expect(mutateAsync).toHaveBeenCalledTimes(1);
    expect(mutateAsync).toHaveBeenCalledWith({ path: PATH, content: `// note\n${SOURCE}` });
    act(() => {
      undo(view());
    });
    expect(view().state.doc.toString()).toBe(SOURCE);
  });

  it("doesn't save a file that was only opened, even one with mixed line breaks", async () => {
    renderEditor({ content: "one\r\ntwo\nthree\r\n" });
    await advance(3000);
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("keeps a CRLF file's line breaks when it saves", async () => {
    vi.mocked(fetchFileContent).mockImplementation(async () => serverFile(CRLF_SOURCE));
    const { view } = renderEditor({ content: CRLF_SOURCE });
    type(view(), view().state.doc.line(2).to, " // two");
    await advance(1000);
    expect(mutateAsync).toHaveBeenCalledWith({
      path: PATH,
      content: "const alpha = 1;\r\nconst beta = 2; // two\r\n",
    });
  });

  it("saves at once with ⌘S and when focus leaves", async () => {
    const { view } = renderEditor();
    type(view(), 0, "a");
    await act(async () => {
      fireEvent.keyDown(
        view().contentDOM,
        /Mac/.test(navigator.platform) ? { key: "s", metaKey: true } : { key: "s", ctrlKey: true },
      );
    });
    expect(mutateAsync).toHaveBeenCalledTimes(1);

    vi.mocked(fetchFileContent).mockImplementation(async () => serverFile(`a${SOURCE}`));
    type(view(), 0, "b");
    await act(async () => {
      fireEvent.blur(view().contentDOM);
    });
    expect(mutateAsync).toHaveBeenCalledTimes(2);
    expect(mutateAsync).toHaveBeenLastCalledWith({ path: PATH, content: `ba${SOURCE}` });
  });

  it("is clean again after undoing every edit, and saves nothing", async () => {
    const onDirtyChange = vi.fn();
    const { view } = renderEditor({ onDirtyChange });
    type(view(), 0, "x");
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    act(() => {
      undo(view());
    });
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    expect(view().state.doc.toString()).toBe(SOURCE);
    await advance(3000);
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("reports unsaved edits to the toolbar's status chip", () => {
    const onSaveStatusChange = vi.fn();
    const { view } = renderEditor({ onSaveStatusChange });
    expect(onSaveStatusChange).toHaveBeenLastCalledWith("idle");
    type(view(), 0, "x");
    expect(onSaveStatusChange).toHaveBeenLastCalledWith("unsaved");
  });

  it("stays read-only without edit permission or for a truncated file", async () => {
    // ⌘K greys out the edit commands with the reason.
    const deleteLines = () =>
      Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .find((command) => command.id === "file.deleteLines");

    vi.mocked(permissions.useCanEdit).mockReturnValue(false);
    const readOnly = renderEditor();
    expect(readOnly.view().contentDOM).toHaveAttribute("aria-readonly", "true");
    expect(deleteLines()?.availability).toEqual({
      available: false,
      reason: NO_EDIT_ACCESS_REASON,
    });
    readOnly.unmount();

    vi.mocked(permissions.useCanEdit).mockReturnValue(true);
    const truncated = renderEditor({ truncated: true });
    expect(truncated.view().contentDOM).toHaveAttribute("aria-readonly", "true");
    expect(deleteLines()?.availability).toEqual({
      available: false,
      reason: TRUNCATED_READ_ONLY_REASON,
    });
    await advance(3000);
    expect(mutateAsync).not.toHaveBeenCalled();
  });
});

describe("CodeMirrorFileEditor when the file changes on disk", () => {
  it("loads the new content into a clean buffer, keeping the cursor, and undo can't revert it", () => {
    const { view, rerender } = renderEditor();
    act(() => view().dispatch({ selection: { anchor: 6 } }));
    rerender({ content: `${SOURCE}const gamma = 3;\n` });
    expect(view().state.doc.line(3).text).toBe("const gamma = 3;");
    expect(view().state.selection.main.head).toBe(6);
    act(() => {
      undo(view());
    });
    expect(view().state.doc.line(3).text).toBe("const gamma = 3;");
  });

  it("asks before replacing unsaved edits, and loads the latest on request", () => {
    const { view, rerender } = renderEditor();
    type(view(), 0, "mine ");
    rerender({ content: `theirs ${SOURCE}` });
    expect(
      screen.getByText("This file was modified externally while you were editing."),
    ).toBeInTheDocument();
    expect(view().state.doc.toString()).toBe(`mine ${SOURCE}`);
    fireEvent.click(screen.getByRole("button", { name: "Load latest" }));
    expect(view().state.doc.toString()).toBe(`theirs ${SOURCE}`);
    expect(screen.queryByText(/modified externally/)).toBeNull();
  });

  it("keeps unsaved edits when the reader chooses Keep mine", () => {
    const { view, rerender } = renderEditor();
    type(view(), 0, "mine ");
    rerender({ content: `theirs ${SOURCE}` });
    fireEvent.click(screen.getByRole("button", { name: "Keep mine" }));
    expect(view().state.doc.toString()).toBe(`mine ${SOURCE}`);
    expect(screen.getByText(/Save your changes to enable commenting/)).toBeInTheDocument();
  });
});

describe("CodeMirrorFileEditor editing keys and comments", () => {
  it("matches brackets around the cursor and indents with Tab", () => {
    const { view, container } = renderEditor();
    const open = SOURCE.indexOf("(");
    act(() => {
      view().focus();
      view().dispatch({ selection: { anchor: open + 1 } });
    });
    expect(container.querySelectorAll(".cm-matchingBracket")).toHaveLength(2);

    act(() => view().dispatch({ selection: { anchor: 0 } }));
    fireEvent.keyDown(view().contentDOM, { key: "Tab" });
    expect(view().state.doc.line(1).text).toMatch(/^\s+const alpha/);
  });

  it("pauses new comments while there are unsaved edits", () => {
    const { view } = renderEditor({
      commentOptions: {
        comments: [],
        activeSelection: null,
        onSetActiveSelection: () => {},
        canComment: true,
      },
    });
    const selectionCommands = () =>
      Object.values(usePaletteCommandsStore.getState().groups).filter(
        (group) => group.heading === "Selection",
      );
    act(() => view().dispatch({ selection: { anchor: 0, head: 5 } }));
    expect(selectionCommands()).toHaveLength(1);
    type(view(), view().state.doc.length, "// more");
    act(() => view().dispatch({ selection: { anchor: 0, head: 5 } }));
    expect(selectionCommands()).toHaveLength(0);
    expect(screen.getByText(/Save your changes to enable commenting/)).toBeInTheDocument();
  });
});

describe("CodeMirrorFileEditor copying", () => {
  function copied(view: EditorView): string | undefined {
    const setData = vi.fn();
    const event = new Event("copy", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: { setData } });
    view.contentDOM.dispatchEvent(event);
    return setData.mock.calls.at(-1)?.[1];
  }
  const select = (view: EditorView, from: number, to: number) =>
    act(() => view.dispatch({ selection: { anchor: from, head: to } }));

  it.each([
    ["an insert", { from: 0, insert: "NEW" }, [0, 3], "NEW"],
    ["a delete", { from: 0, to: 6 }, [0, 5], "alpha"],
    ["a replace", { from: 6, to: 11, insert: "omega" }, [6, 11], "omega"],
  ])("copies the edited text after %s that isn't saved yet", (_label, change, range, expected) => {
    const { view } = renderEditor();
    act(() => view().dispatch({ changes: change, userEvent: "input" }));
    select(view(), range[0]!, range[1]!);
    expect(copied(view())).toBe(expected);
  });

  it("keeps CRLF breaks when copying edited lines", () => {
    const { view } = renderEditor({ content: CRLF_SOURCE });
    type(view(), 0, "// top\n");
    select(view(), 0, view().state.doc.line(2).to);
    expect(copied(view())).toBe("// top\r\nconst alpha = 1;");
  });

  it("copies the newest text when an edit follows a save that is still in flight", async () => {
    let finishWrite: () => void = () => {};
    mutateAsync.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          finishWrite = resolve;
        }),
    );
    const { view } = renderEditor();
    type(view(), 0, "A");
    await advance(1000);
    expect(mutateAsync).toHaveBeenCalledTimes(1);
    type(view(), 0, "B");
    select(view(), 0, 2);
    expect(copied(view())).toBe("BA");
    await act(async () => finishWrite());
    select(view(), 0, 2);
    expect(copied(view())).toBe("BA");
  });
});
