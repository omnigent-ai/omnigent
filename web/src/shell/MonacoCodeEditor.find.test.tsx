// Tests for the MonacoCodeEditor "Find in file" toggle wiring.
//
// The toolbar's Find button flips a `searchOpen` prop; the editor mirrors
// Monaco's native find widget to it (open when set, close when cleared) and
// reports back via onSearchHandled when find is closed from within Monaco
// (Escape / the widget's ✕) so the toolbar toggle stays in sync.
//
// Monaco can't mount in jsdom, so @monaco-editor/react's Editor is mocked to
// invoke onMount with a thin fake editor exposing the slice of API the find
// wiring drives: getAction("actions.find").run(), and getContribution(...) →
// a fake find controller (getState().isRevealed, closeFindWidget,
// onFindReplaceStateChange). The comment layer and save wiring are irrelevant
// here, so they're mocked out.

import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Mock } from "vitest";

const h = vi.hoisted(() => ({
  onChange: null as ((value: string | undefined, ev: unknown) => void) | null,
}));

const fakeMonaco = {
  editor: { EndOfLineSequence: { LF: 1, CRLF: 2 } },
  KeyMod: { CtrlCmd: 2048 },
  KeyCode: { KeyS: 49 },
};

// Fake find controller. `isRevealed` is mutable so tests can model the widget's
// real open/close state; onFindReplaceStateChange captures the listener so a
// test can simulate a Monaco-initiated close.
interface FakeFindController {
  isRevealed: boolean;
  run: Mock<() => void>;
  closeFindWidget: Mock<() => void>;
  getState: () => {
    isRevealed: boolean;
    onFindReplaceStateChange: (listener: (e: { isRevealed: boolean }) => void) => {
      dispose: () => void;
    };
  };
  fireStateChange: (e: { isRevealed: boolean }) => void;
  dispose: Mock<() => void>;
}

function makeFakeController(): FakeFindController {
  let listener: ((e: { isRevealed: boolean }) => void) | null = null;
  const dispose = vi.fn(() => {
    listener = null;
  });
  const controller: FakeFindController = {
    isRevealed: false,
    run: vi.fn(() => {
      controller.isRevealed = true;
    }),
    closeFindWidget: vi.fn(() => {
      controller.isRevealed = false;
    }),
    getState: () => ({
      get isRevealed() {
        return controller.isRevealed;
      },
      onFindReplaceStateChange: (l) => {
        listener = l;
        return { dispose };
      },
    }),
    fireStateChange: (e) => listener?.(e),
    dispose,
  };
  return controller;
}

let fakeController: FakeFindController;

interface FakeEditor {
  getValue: () => string;
  setValue: (v: string) => void;
  getModel: () => { setEOL: () => void };
  addCommand: () => void;
  onDidBlurEditorWidget: () => { dispose: () => void };
  saveViewState: () => null;
  restoreViewState: () => void;
  getAction: (id: string) => { run: () => void } | undefined;
  getContribution: (id: string) => FakeFindController | null;
}

function makeFakeEditor(initial: string): FakeEditor {
  let value = initial;
  return {
    getValue: () => value,
    setValue: (v) => {
      value = v;
    },
    getModel: () => ({ setEOL: () => {} }),
    addCommand: () => {},
    onDidBlurEditorWidget: () => ({ dispose: () => {} }),
    saveViewState: () => null,
    restoreViewState: () => {},
    // Only the find action is exercised here.
    getAction: (id) => (id === "actions.find" ? { run: fakeController.run } : undefined),
    getContribution: () => fakeController,
  };
}

let fakeEditor: FakeEditor | null = null;

vi.mock("@monaco-editor/react", async () => {
  const { useEffect } = await import("react");
  return {
    Editor: (props: {
      onMount?: (editor: unknown, monaco: unknown) => void;
      onChange?: (value: string | undefined, ev: unknown) => void;
    }) => {
      h.onChange = props.onChange ?? null;
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
import * as writeHook from "@/hooks/useWriteFileContent";
import * as runnerHook from "@/hooks/RunnerHealthProvider";

const PATH = "src/a.ts";
const INITIAL = "const x = 1;\n";

function makeEditor(props: { searchOpen?: boolean; onSearchHandled?: () => void } = {}) {
  return (
    <MonacoCodeEditor
      content={INITIAL}
      conversationId="conv_monaco_find"
      path={PATH}
      isSettled={true}
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={() => {}}
      {...props}
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
  fakeEditor = makeFakeEditor(INITIAL);
  fakeController = makeFakeController();
  h.onChange = null;

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

describe("MonacoCodeEditor find toggle", () => {
  it("opens Monaco's native find when searchOpen is set", async () => {
    await renderMounted(makeEditor({ searchOpen: true }));
    // The toolbar Find button (searchOpen=true) runs Monaco's find action.
    expect(fakeController.run).toHaveBeenCalledTimes(1);
    expect(fakeController.closeFindWidget).not.toHaveBeenCalled();
  });

  it("does not open find while searchOpen is false", async () => {
    await renderMounted(makeEditor({ searchOpen: false }));
    expect(fakeController.run).not.toHaveBeenCalled();
    // No open widget → nothing to close.
    expect(fakeController.closeFindWidget).not.toHaveBeenCalled();
  });

  it("closes the open find widget when searchOpen flips back to false", async () => {
    const { rerender } = await renderMounted(makeEditor({ searchOpen: true }));
    expect(fakeController.run).toHaveBeenCalledTimes(1);
    // Widget is now open (run() set isRevealed). Re-clicking Find toggles the
    // prop to false, which must close the widget — not open a second time.
    await act(async () => {
      rerender(makeEditor({ searchOpen: false }));
    });
    expect(fakeController.closeFindWidget).toHaveBeenCalledTimes(1);
    expect(fakeController.run).toHaveBeenCalledTimes(1);
  });

  it("does not call closeFindWidget when the widget is already closed", async () => {
    const { rerender } = await renderMounted(makeEditor({ searchOpen: false }));
    // Flip false→true→false but simulate the widget never actually revealing
    // (controller stays closed). The close guard reads isRevealed, so no
    // redundant close fires.
    fakeController.isRevealed = false;
    await act(async () => {
      rerender(makeEditor({ searchOpen: false }));
    });
    expect(fakeController.closeFindWidget).not.toHaveBeenCalled();
  });

  it("reports back via onSearchHandled when find is closed from within Monaco", async () => {
    const onSearchHandled = vi.fn();
    await renderMounted(makeEditor({ searchOpen: true, onSearchHandled }));
    // Widget is open. Simulate the user pressing Escape / clicking the widget's
    // ✕: Monaco flips isRevealed to false and fires a state change whose
    // isRevealed flag marks that field as changed.
    fakeController.isRevealed = false;
    act(() => {
      fakeController.fireStateChange({ isRevealed: true });
    });
    // The toolbar toggle is reset so the next click re-opens instead of no-op.
    expect(onSearchHandled).toHaveBeenCalledTimes(1);
  });

  it("ignores state changes that are not a find-widget close", async () => {
    const onSearchHandled = vi.fn();
    await renderMounted(makeEditor({ searchOpen: true, onSearchHandled }));
    // A match-count/position change (isRevealed flag not set) must not reset the
    // toggle — only an actual close does.
    act(() => {
      fakeController.fireStateChange({ isRevealed: false });
    });
    expect(onSearchHandled).not.toHaveBeenCalled();
  });

  it("disposes the state-change subscription on unmount", async () => {
    const { unmount } = await renderMounted(makeEditor({ searchOpen: false }));
    await act(async () => {
      unmount();
    });
    expect(fakeController.dispose).toHaveBeenCalled();
  });
});
