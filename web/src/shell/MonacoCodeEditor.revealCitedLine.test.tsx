// Monaco is mocked in jsdom: verify navigation and layout lifecycle here;
// browser tests verify actual centering and collapsed diff expansion.

import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fakeMonaco = {
  editor: { EndOfLineSequence: { LF: 1, CRLF: 2 } },
  KeyMod: { CtrlCmd: 2048 },
  KeyCode: { KeyS: 49 },
};

interface FakeEditor {
  getValue: () => string;
  setValue: (v: string) => void;
  getModel: () => {
    setEOL: () => void;
    getLineCount: () => number;
    validatePosition: (p: { lineNumber: number; column: number }) => {
      lineNumber: number;
      column: number;
    };
  };
  getLayoutInfo: () => { width: number; height: number };
  onDidLayoutChange: (listener: () => void) => { dispose: () => void };
  resize: (width: number, height: number) => void;
  addCommand: () => void;
  onDidBlurEditorWidget: () => { dispose: () => void };
  setScrollTop: (top: number) => void;
  onDidScrollChange: () => { dispose: () => void };
  getDomNode: () => HTMLElement;
  saveViewState: () => null;
  restoreViewState: () => void;
  getAction: () => undefined;
  getContribution: () => null;
  revealPositionInCenter: ReturnType<typeof vi.fn>;
  setPosition: ReturnType<typeof vi.fn>;
  scrollTops: number[];
}

function makeFakeEditor(initial: string, lineCount: number): FakeEditor {
  const dom = document.createElement("div");
  let width = 600;
  let height = 800;
  const layouts = new Set<() => void>();
  const editor: FakeEditor = {
    getValue: () => initial,
    setValue: () => {},
    getModel: () => ({
      setEOL: () => {},
      getLineCount: () => lineCount,
      validatePosition: (p) => ({ ...p, lineNumber: Math.min(p.lineNumber, lineCount) }),
    }),
    getLayoutInfo: () => ({ width, height }),
    onDidLayoutChange: (listener) => {
      layouts.add(listener);
      return {
        dispose: () => {
          layouts.delete(listener);
        },
      };
    },
    resize: (nextWidth, nextHeight) => {
      width = nextWidth;
      height = nextHeight;
      for (const listener of layouts) listener();
    },
    addCommand: () => {},
    onDidBlurEditorWidget: () => ({ dispose: () => {} }),
    setScrollTop: (top) => {
      editor.scrollTops.push(top);
    },
    onDidScrollChange: () => ({ dispose: () => {} }),
    getDomNode: () => dom,
    saveViewState: () => null,
    restoreViewState: () => {},
    getAction: () => undefined,
    getContribution: () => null,
    revealPositionInCenter: vi.fn(),
    setPosition: vi.fn(),
    scrollTops: [],
  };
  return editor;
}

let fakeEditor: FakeEditor | null = null;

vi.mock("@monaco-editor/react", async () => {
  const { useEffect } = await import("react");
  return {
    Editor: (props: { onMount?: (editor: unknown, monaco: unknown) => void }) => {
      useEffect(() => {
        props.onMount?.(fakeEditor, fakeMonaco);
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, []);
      return null;
    },
  };
});

vi.mock("./monacoSetup", () => ({
  ensureMonacoReady: vi.fn(() => Promise.resolve()),
  ensureLanguage: vi.fn(() => Promise.resolve()),
  monacoLanguageId: vi.fn((lang: string) => lang),
  resolvedThemeToMonaco: vi.fn(() => "github-light"),
}));
vi.mock("./useMonacoCommentLayer", () => ({ useMonacoCommentLayer: () => null }));
vi.mock("next-themes", () => ({ useTheme: () => ({ resolvedTheme: "light" }) }));
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn().mockReturnValue(true) }));
vi.mock("@/hooks/useWriteFileContent", () => ({ useWriteFileContent: vi.fn() }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(),
  useSessionHostOnline: vi.fn(),
}));

import { MonacoCodeEditor } from "./MonacoCodeEditor";
import { saveScrollTop } from "./useScrollRestore";
import * as writeHook from "@/hooks/useWriteFileContent";
import * as runnerHook from "@/hooks/RunnerHealthProvider";

const CONV = "conv_monaco_reveal";
const PATH = "src/module.py";
const LINE_COUNT = 400;
const CONTENT = Array.from({ length: LINE_COUNT }, (_, i) => `filler_${i + 1}`).join("\n");

function makeEditor(
  revealLine?: number,
  { column, truncated = false }: { column?: number; truncated?: boolean } = {},
) {
  return (
    <MonacoCodeEditor
      content={CONTENT}
      conversationId={CONV}
      path={PATH}
      isSettled={true}
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={() => {}}
      position={revealLine == null ? undefined : { line: revealLine, column }}
      truncated={truncated}
    />
  );
}

// Render and flush the ready promise so <Editor> mounts and onMount fires.
async function renderMounted(el: React.ReactElement) {
  const utils = render(el);
  await act(async () => {});
  return utils;
}

beforeEach(() => {
  fakeEditor = makeFakeEditor(CONTENT, LINE_COUNT);
  vi.mocked(writeHook.useWriteFileContent).mockReturnValue({
    isPending: false,
    isError: false,
    reset: vi.fn(),
    mutateAsync: vi.fn().mockResolvedValue(undefined),
  } as unknown as ReturnType<typeof writeHook.useWriteFileContent>);
  vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(true);
});

afterEach(() => {
  vi.clearAllMocks();
  fakeEditor = null;
});

describe("MonacoCodeEditor cited-line reveal", () => {
  it("does not reveal anything without a citation", async () => {
    await renderMounted(makeEditor());

    expect(fakeEditor!.revealPositionInCenter).not.toHaveBeenCalled();
    expect(fakeEditor!.setPosition).not.toHaveBeenCalled();
  });

  it.each([false, true])("clamps to the last loaded line (truncated=%s)", async (truncated) => {
    await renderMounted(makeEditor(9999, { truncated }));

    expect(fakeEditor!.revealPositionInCenter).toHaveBeenCalledWith(
      { lineNumber: LINE_COUNT, column: 1 },
      1,
    );
  });

  it("skips the saved-scroll restore so it cannot fight the reveal", async () => {
    // A remembered offset for this file would normally be re-asserted for the
    // whole restore budget — dragging the viewer away from the cited line.
    saveScrollTop(`viewer:${CONV}:${PATH}`, 1234);

    await renderMounted(makeEditor(350));

    expect(fakeEditor!.scrollTops).not.toContain(1234);
    expect(fakeEditor!.revealPositionInCenter).toHaveBeenCalledWith(
      { lineNumber: 350, column: 1 },
      1,
    );
  });

  it("still restores the saved offset when there is no citation", async () => {
    saveScrollTop(`viewer:${CONV}:${PATH}`, 1234);

    await renderMounted(makeEditor());

    expect(fakeEditor!.scrollTops).toContain(1234);
  });

  it("restores the reader's scroll after reopening, then accepts a fresh click", async () => {
    const request = makeEditor(100);
    const { unmount } = await renderMounted(request);
    fakeEditor!.getDomNode().dispatchEvent(new Event("wheel"));
    saveScrollTop(`viewer:${CONV}:${PATH}`, 2345);
    unmount();

    fakeEditor = makeFakeEditor(CONTENT, LINE_COUNT);
    const { rerender } = await renderMounted(request);
    expect(fakeEditor.scrollTops).toContain(2345);
    expect(fakeEditor.revealPositionInCenter).not.toHaveBeenCalled();

    rerender(makeEditor(100));
    await act(async () => {});
    expect(fakeEditor.revealPositionInCenter).toHaveBeenCalledWith(
      { lineNumber: 100, column: 1 },
      1,
    );
  });

  it("centers fresh requests on mount and on repeated clicks, including columns", async () => {
    const { rerender } = await renderMounted(makeEditor(350, { column: 7 }));
    expect(fakeEditor!.setPosition).toHaveBeenLastCalledWith({ lineNumber: 350, column: 7 });
    expect(fakeEditor!.revealPositionInCenter).toHaveBeenLastCalledWith(
      { lineNumber: 350, column: 7 },
      1,
    );
    rerender(makeEditor(42));
    expect(fakeEditor!.revealPositionInCenter).toHaveBeenLastCalledWith(
      { lineNumber: 42, column: 1 },
      1,
    );
    rerender(makeEditor(42));
    expect(fakeEditor!.revealPositionInCenter).toHaveBeenCalledTimes(3);
  });

  it("waits for a visible editor and recenters when its panel grows", async () => {
    fakeEditor!.resize(0, 0);
    await renderMounted(makeEditor(1));
    expect(fakeEditor!.revealPositionInCenter).not.toHaveBeenCalled();
    act(() => fakeEditor!.resize(600, 100));
    act(() => fakeEditor!.resize(600, 800));
    expect(fakeEditor!.revealPositionInCenter).toHaveBeenCalledTimes(2);
    expect(fakeEditor!.revealPositionInCenter).toHaveBeenLastCalledWith(
      { lineNumber: 1, column: 1 },
      1,
    );
  });

  it.each(["wheel", "touchstart", "pointerdown", "keydown"])(
    "stops recentering after %s",
    async (type) => {
      await renderMounted(makeEditor(1));
      fakeEditor!.getDomNode().dispatchEvent(new Event(type));
      act(() => fakeEditor!.resize(600, 900));
      expect(fakeEditor!.revealPositionInCenter).toHaveBeenCalledTimes(1);
    },
  );

  it("removes the layout listener when the editor unmounts", async () => {
    const { unmount } = await renderMounted(makeEditor(1));
    const editor = fakeEditor!;
    unmount();
    editor.resize(600, 900);
    expect(editor.revealPositionInCenter).toHaveBeenCalledTimes(1);
  });
});
