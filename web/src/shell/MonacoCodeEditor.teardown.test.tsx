// Regression guard for the save-after-teardown state leak in MonacoCodeEditor
// (the handleSave mountedRef guard).
//
// The outer MonacoCodeEditor holds useMarkdownEditorSync; the inner editor
// (key={editorKey}) is torn down on a path switch / key remount / panel close
// while the sync hook PERSISTS. An auto-save kicked off while the editor was
// alive can resolve AFTER teardown — the network write still lands, but its
// post-write mutations (markSaved / setDirty(false) / dismissExternalUpdate)
// must NOT run, or they leak this file's saved-state into whatever file the
// persistent hook now tracks.
//
// Like the markdown teardown suite: the fake Monaco editor drives the REAL
// useAutoSave wiring (so a save genuinely goes in flight), while
// useMarkdownEditorSync is mocked so its setters are spies. Only the write is a
// controllable deferred.

import { StrictMode } from "react";
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const h = vi.hoisted(() => ({
  onChange: null as ((value: string | undefined, ev: unknown) => void) | null,
}));

const fakeMonaco = {
  editor: { EndOfLineSequence: { LF: 1, CRLF: 2 } },
  KeyMod: { CtrlCmd: 2048 },
  KeyCode: { KeyS: 49 },
};

interface FakeEditor {
  getValue: () => string;
  setValue: (v: string) => void;
  getModel: () => { setEOL: () => void };
  addCommand: () => void;
  onDidBlurEditorWidget: () => { dispose: () => void };
  saveViewState: () => null;
  restoreViewState: () => void;
  getAction: () => { run: () => void };
  getContribution: () => null;
  __set: (v: string) => void;
}

function makeFakeEditor(initial: string): FakeEditor {
  let value = initial;
  return {
    getValue: () => value,
    setValue: (v) => {
      value = v;
      h.onChange?.(v, { isFlush: true });
    },
    getModel: () => ({ setEOL: () => {} }),
    addCommand: () => {},
    onDidBlurEditorWidget: () => ({ dispose: () => {} }),
    saveViewState: () => null,
    restoreViewState: () => {},
    getAction: () => ({ run: () => {} }),
    getContribution: () => null,
    __set: (v) => {
      value = v;
    },
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
// Sync hook is mocked here (unlike the autosave suite) so its setters are spies.
vi.mock("./useMarkdownEditorSync", () => ({ useMarkdownEditorSync: vi.fn() }));

import { MonacoCodeEditor } from "./MonacoCodeEditor";
import * as writeHook from "@/hooks/useWriteFileContent";
import * as runnerHook from "@/hooks/RunnerHealthProvider";
import * as syncHook from "./useMarkdownEditorSync";

const PATH = "src/a.ts";
const INITIAL = "const x = 1;\n";
const EDITED = "const x = 2;\n";

// Stable spies — identity must survive renders so handleSave (which lists them
// in its dependency array) isn't recreated each render.
let setDirty: ReturnType<typeof vi.fn>;
let markSaved: ReturnType<typeof vi.fn>;
let dismissExternalUpdate: ReturnType<typeof vi.fn>;
let reconcileServerContent: ReturnType<typeof vi.fn>;

let resolveWrite: () => void;
let writePromise: Promise<void>;
let mutateAsync: ReturnType<typeof vi.fn>;

function makeEditor() {
  return (
    <MonacoCodeEditor
      content={INITIAL}
      // Not the focused/running session → no pre-write GET.
      conversationId="conv_monaco_teardown"
      path={PATH}
      isSettled={true}
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={() => {}}
    />
  );
}

// Render and flush the ready promise so <Editor> mounts and onMount fires.
async function renderMounted(el: React.ReactElement) {
  const utils = render(el);
  await act(async () => {});
  return utils;
}

// Drive an edit + fire the debounce so a save goes in flight (deferred write).
async function editAndStartSave() {
  fakeEditor!.__set(EDITED);
  await act(async () => {
    h.onChange?.(EDITED, {});
  });
  await act(async () => {
    vi.advanceTimersByTime(1000);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  fakeEditor = makeFakeEditor(INITIAL);
  h.onChange = null;

  setDirty = vi.fn();
  markSaved = vi.fn();
  dismissExternalUpdate = vi.fn();
  reconcileServerContent = vi.fn().mockReturnValue(false);
  vi.mocked(syncHook.useMarkdownEditorSync).mockReturnValue({
    editorKey: 1,
    isDirty: false,
    setDirty,
    hasExternalUpdate: false,
    discardAndApplyExternal: vi.fn(),
    dismissExternalUpdate,
    markSaved,
    reconcileServerContent,
  } as unknown as ReturnType<typeof syncHook.useMarkdownEditorSync>);

  writePromise = new Promise<void>((r) => {
    resolveWrite = r;
  });
  mutateAsync = vi.fn().mockReturnValue(writePromise);
  vi.mocked(writeHook.useWriteFileContent).mockReturnValue({
    isPending: false,
    isError: false,
    reset: vi.fn(),
    mutateAsync,
  } as unknown as ReturnType<typeof writeHook.useWriteFileContent>);

  vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(true);
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  fakeEditor = null;
});

describe("MonacoCodeEditor save-after-teardown guard", () => {
  it("does NOT run post-write hook mutations when the write resolves after unmount", async () => {
    const { unmount } = await renderMounted(makeEditor());
    await editAndStartSave();
    // Save in flight (deferred write); markSaved must not fire yet.
    expect(mutateAsync).toHaveBeenCalledWith({ path: PATH, content: EDITED });
    expect(markSaved).not.toHaveBeenCalled();

    await act(async () => {
      unmount();
    });
    // Write lands after teardown.
    await act(async () => {
      resolveWrite();
      await writePromise;
    });

    // The leak: a late markSaved()/dismissExternalUpdate() would mutate the
    // persistent sync hook (now tracking another file). The mountedRef guard
    // must suppress them. Removing the guard makes both fire here → red.
    expect(markSaved).not.toHaveBeenCalled();
    expect(dismissExternalUpdate).not.toHaveBeenCalled();
  });

  it("DOES run post-write hook mutations for a save that resolves while still mounted", async () => {
    // Positive control: proves the negative test isn't vacuous — the same path
    // genuinely calls markSaved/setDirty(false) when the editor is still alive.
    await renderMounted(makeEditor());
    await editAndStartSave();
    await act(async () => {
      resolveWrite();
      await writePromise;
    });

    // Echo-dedupe marker recorded and editor forced clean — the normal save.
    expect(markSaved).toHaveBeenCalledWith(EDITED);
    expect(setDirty).toHaveBeenCalledWith(false);
    expect(dismissExternalUpdate).toHaveBeenCalledOnce();
  });

  it("still runs post-write mutations after a StrictMode mount/unmount/remount", async () => {
    // Regression: mountedRef must be set true in the effect SETUP, not only
    // cleared in cleanup. StrictMode (on in dev) runs mount→unmount→remount; a
    // cleanup-only ref sticks at false after that cycle, so every save's
    // post-write clear is skipped → stuck "Unsaved" + autosave loop. RTL's
    // render() isn't StrictMode by default, so this must be opted in explicitly.
    await renderMounted(<StrictMode>{makeEditor()}</StrictMode>);
    await editAndStartSave();
    await act(async () => {
      resolveWrite();
      await writePromise;
    });

    // With a cleanup-only mountedRef these are skipped (the guard bails on the
    // still-mounted editor because the ref is stuck false post-remount).
    expect(markSaved).toHaveBeenCalledWith(EDITED);
    expect(setDirty).toHaveBeenCalledWith(false);
  });
});
