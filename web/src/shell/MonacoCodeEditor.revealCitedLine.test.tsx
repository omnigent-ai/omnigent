// Tests for the MonacoCodeEditor cited-line reveal wiring.
//
// A chat citation (`path:line`) opens the file viewer with a `revealLine`;
// the editor must land on that line (centered) instead of parking at the top,
// must not fight the reveal with the saved-scroll restore, and must re-reveal
// when a later citation targets a different line of the same open file.
//
// Monaco can't mount in jsdom, so @monaco-editor/react's Editor is mocked to
// invoke onMount with a thin fake editor exposing the slice of API the reveal
// drives: revealLineInCenter, setPosition, getModel().getLineCount(), and the
// scroll-restore hooks (setScrollTop / onDidScrollChange). The comment layer
// and save wiring are irrelevant here, so they're mocked out.

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
  getModel: () => { setEOL: () => void; getLineCount: () => number };
  addCommand: () => void;
  onDidBlurEditorWidget: () => { dispose: () => void };
  setScrollTop: (top: number) => void;
  onDidScrollChange: () => { dispose: () => void };
  getDomNode: () => HTMLElement;
  saveViewState: () => null;
  restoreViewState: () => void;
  getAction: () => undefined;
  getContribution: () => null;
  revealLineInCenter: ReturnType<typeof vi.fn>;
  setPosition: ReturnType<typeof vi.fn>;
  scrollTops: number[];
}

function makeFakeEditor(initial: string, lineCount: number): FakeEditor {
  const editor: FakeEditor = {
    getValue: () => initial,
    setValue: () => {},
    getModel: () => ({ setEOL: () => {}, getLineCount: () => lineCount }),
    addCommand: () => {},
    onDidBlurEditorWidget: () => ({ dispose: () => {} }),
    setScrollTop: (top) => {
      editor.scrollTops.push(top);
    },
    onDidScrollChange: () => ({ dispose: () => {} }),
    getDomNode: () => document.createElement("div"),
    saveViewState: () => null,
    restoreViewState: () => {},
    getAction: () => undefined,
    getContribution: () => null,
    revealLineInCenter: vi.fn(),
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
vi.mock("@/hooks/RunnerHealthProvider", () => ({ useSessionRunnerOnline: vi.fn() }));

import { MonacoCodeEditor } from "./MonacoCodeEditor";
import { saveScrollTop } from "./useScrollRestore";
import * as writeHook from "@/hooks/useWriteFileContent";
import * as runnerHook from "@/hooks/RunnerHealthProvider";

const CONV = "conv_monaco_reveal";
const PATH = "src/module.py";
const LINE_COUNT = 400;
const CONTENT = Array.from({ length: LINE_COUNT }, (_, i) => `filler_${i + 1}`).join("\n");

function makeEditor(revealLine?: number | null) {
  return (
    <MonacoCodeEditor
      content={CONTENT}
      conversationId={CONV}
      path={PATH}
      isSettled={true}
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={() => {}}
      revealLine={revealLine}
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
  it("reveals the cited line (centered) on mount", async () => {
    await renderMounted(makeEditor(350));

    expect(fakeEditor!.revealLineInCenter).toHaveBeenCalledWith(350);
    expect(fakeEditor!.setPosition).toHaveBeenCalledWith({ lineNumber: 350, column: 1 });
  });

  it("does not reveal anything without a citation", async () => {
    await renderMounted(makeEditor());

    expect(fakeEditor!.revealLineInCenter).not.toHaveBeenCalled();
    expect(fakeEditor!.setPosition).not.toHaveBeenCalled();
  });

  it("clamps a cited line past the end of the file to the last line", async () => {
    await renderMounted(makeEditor(9999));

    expect(fakeEditor!.revealLineInCenter).toHaveBeenCalledWith(LINE_COUNT);
  });

  it("skips the saved-scroll restore so it cannot fight the reveal", async () => {
    // A remembered offset for this file would normally be re-asserted for the
    // whole restore budget — dragging the viewer away from the cited line.
    saveScrollTop(`viewer:${CONV}:${PATH}`, 1234);

    await renderMounted(makeEditor(350));

    expect(fakeEditor!.scrollTops).not.toContain(1234);
    expect(fakeEditor!.revealLineInCenter).toHaveBeenCalledWith(350);
  });

  it("still restores the saved offset when there is no citation", async () => {
    saveScrollTop(`viewer:${CONV}:${PATH}`, 1234);

    await renderMounted(makeEditor());

    expect(fakeEditor!.scrollTops).toContain(1234);
  });

  it("re-reveals when a later citation targets a different line", async () => {
    const view = await renderMounted(makeEditor(350));
    fakeEditor!.revealLineInCenter.mockClear();

    view.rerender(makeEditor(42));
    await act(async () => {});

    expect(fakeEditor!.revealLineInCenter).toHaveBeenCalledWith(42);
  });
});
