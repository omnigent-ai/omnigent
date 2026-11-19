import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { useState, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDictationInsert } from "@/hooks/useDictationInsert";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { ChatComposer, ComposerEngineInput } from "../ChatComposer";
import * as CodeMirrorModule from "./codeMirrorComposerModule";
import {
  codeMirrorComposerLoad,
  loadCodeMirrorComposer,
  resetCodeMirrorComposerLoadForTests,
} from "./codeMirrorComposerLoader";
import {
  COMPOSER_LOAD_RETRY_INTERVAL_MS,
  effectiveComposerEngine,
  useRequestedComposerEngine,
} from "./composerEnginePolicy";
import {
  composerTargetOf,
  type ComposerInputEngine,
  type ComposerTextTarget,
} from "./composerInput";

const mobile = vi.hoisted(() => ({ current: false }));
vi.mock("@/hooks/useIsMobileViewport", () => ({
  useIsMobileViewport: () => mobile.current,
}));

const KEYBOARD = { submitWithModEnter: false, preventsKeyboardSubmit: false } as const;

function flagWrapper(enabled: boolean) {
  const info = { ...FALLBACK_SERVER_INFO, features: { chat_composer_codemirror: enabled } };
  return function Wrapper({ children }: { children: ReactNode }) {
    return <CapabilitiesProvider info={info}>{children}</CapabilitiesProvider>;
  };
}

async function loadedCodeMirror() {
  resetCodeMirrorComposerLoadForTests(async () => CodeMirrorModule);
  loadCodeMirrorComposer();
  await waitFor(() => expect(codeMirrorComposerLoad().status).toBe("ready"));
}

function Harness({
  engine,
  initial = "",
  dictating = false,
  autoFocus = false,
}: {
  engine: ComposerInputEngine;
  initial?: string;
  dictating?: boolean;
  autoFocus?: boolean;
}) {
  const [value, setValue] = useState(initial);
  return (
    <ChatComposer
      engine={engine}
      dictating={dictating}
      keyboard={KEYBOARD}
      input={{
        "aria-label": "Message the agent",
        value,
        autoFocus,
        onChange: (event) => setValue(event.currentTarget.value),
      }}
      actions={{ leading: null, trailing: null }}
    />
  );
}

/** A composer beside another control, which takes focus away from the composer. */
function ComposerBesideButton({
  engine,
  autoFocus,
}: {
  engine: ComposerInputEngine;
  autoFocus: boolean;
}) {
  return (
    <>
      <Harness engine={engine} autoFocus={autoFocus} initial="draft" />
      <button type="button">Elsewhere</button>
    </>
  );
}

function composerInput(): HTMLElement {
  return document.querySelector<HTMLElement>("[data-composer-input]")!;
}

function engineOf(element: HTMLElement): ComposerInputEngine {
  return element instanceof HTMLTextAreaElement ? "textarea" : "codemirror";
}

beforeEach(() => {
  mobile.current = false;
  resetCodeMirrorComposerLoadForTests();
});
afterEach(() => {
  resetCodeMirrorComposerLoadForTests();
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

describe("effectiveComposerEngine", () => {
  it.each([
    [false, false, "ready", "textarea"],
    [true, true, "ready", "textarea"],
    [true, false, "idle", "textarea"],
    [true, false, "loading", "textarea"],
    [true, false, "failed", "textarea"],
    [true, false, "ready", "codemirror"],
  ] as const)(
    "flag %s, mobile %s, chunk %s runs the %s engine",
    (flagOn, mobileViewport, load, engine) => {
      expect(effectiveComposerEngine(flagOn, mobileViewport, load)).toBe(engine);
    },
  );
});

describe("requesting the engine", () => {
  it("stays on the textarea and loads nothing with the flag off", () => {
    const importer = vi.fn(async () => CodeMirrorModule);
    resetCodeMirrorComposerLoadForTests(importer);
    const { result } = renderHook(() => useRequestedComposerEngine(), {
      wrapper: flagWrapper(false),
    });
    expect(result.current).toBe("textarea");
    expect(importer).not.toHaveBeenCalled();
  });

  it("stays on the textarea and loads nothing on a mobile viewport", () => {
    mobile.current = true;
    const importer = vi.fn(async () => CodeMirrorModule);
    resetCodeMirrorComposerLoadForTests(importer);
    const { result } = renderHook(() => useRequestedComposerEngine(), {
      wrapper: flagWrapper(true),
    });
    expect(result.current).toBe("textarea");
    expect(importer).not.toHaveBeenCalled();
  });

  it("loads the chunk with the flag on and switches once it is ready", async () => {
    const importer = vi.fn(async () => CodeMirrorModule);
    resetCodeMirrorComposerLoadForTests(importer);
    const { result } = renderHook(() => useRequestedComposerEngine(), {
      wrapper: flagWrapper(true),
    });
    await waitFor(() => expect(result.current).toBe("codemirror"));
    expect(importer).toHaveBeenCalledTimes(1);
  });

  it("retries a failed load on window focus, at most once a minute", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    let now = Date.now();
    vi.spyOn(Date, "now").mockImplementation(() => now);
    let online = false;
    const importer = vi.fn(async () => {
      if (!online) throw new Error("offline");
      return CodeMirrorModule;
    });
    resetCodeMirrorComposerLoadForTests(importer);
    const { result } = renderHook(() => useRequestedComposerEngine(), {
      wrapper: flagWrapper(true),
    });
    await waitFor(() => expect(codeMirrorComposerLoad().status).toBe("failed"));
    expect(result.current).toBe("textarea");

    act(() => window.dispatchEvent(new Event("focus")));
    expect(importer).toHaveBeenCalledTimes(1);

    now += COMPOSER_LOAD_RETRY_INTERVAL_MS;
    await act(async () => window.dispatchEvent(new Event("focus")));
    expect(importer).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(codeMirrorComposerLoad().status).toBe("failed"));

    act(() => window.dispatchEvent(new Event("focus")));
    expect(importer).toHaveBeenCalledTimes(2);

    online = true;
    now += COMPOSER_LOAD_RETRY_INTERVAL_MS;
    await act(async () => window.dispatchEvent(new Event("focus")));
    await waitFor(() => expect(codeMirrorComposerLoad().status).toBe("ready"));
    expect(result.current).toBe("codemirror");
  });
});

describe("switching a mounted composer's engine", () => {
  it("keeps the draft, selection and focus when switching to CodeMirror and back", async () => {
    await loadedCodeMirror();
    const { rerender } = render(<Harness engine="textarea" initial="one two three" />);
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    act(() => {
      textarea.focus();
      textarea.setSelectionRange(4, 7);
    });

    rerender(<Harness engine="codemirror" initial="one two three" />);

    const editor = composerInput();
    expect(engineOf(editor)).toBe("codemirror");
    const surface = composerTargetOf(editor)!;
    expect(surface.value).toBe("one two three");
    expect([surface.selectionStart, surface.selectionEnd]).toEqual([4, 7]);
    expect(document.activeElement).toBe(editor);

    rerender(<Harness engine="textarea" initial="one two three" />);

    const back = composerInput() as HTMLTextAreaElement;
    expect(engineOf(back)).toBe("textarea");
    expect(back.value).toBe("one two three");
    expect([back.selectionStart, back.selectionEnd]).toEqual([4, 7]);
    expect(document.activeElement).toBe(back);
  });

  it("doesn't take focus it didn't have", async () => {
    await loadedCodeMirror();
    const { rerender } = render(<Harness engine="textarea" initial="draft" />);
    rerender(<Harness engine="codemirror" initial="draft" />);
    expect(engineOf(composerInput())).toBe("codemirror");
    expect(document.activeElement).toBe(document.body);
  });

  it("waits for an IME composition to end before switching", async () => {
    await loadedCodeMirror();
    const { rerender } = render(<Harness engine="textarea" initial="にほ" />);
    const textarea = screen.getByRole("textbox");
    fireEvent.compositionStart(textarea);

    rerender(<Harness engine="codemirror" initial="にほ" />);
    expect(engineOf(composerInput())).toBe("textarea");

    fireEvent.compositionEnd(textarea);
    await waitFor(() => expect(engineOf(composerInput())).toBe("codemirror"));
  });

  it("waits while dictation streams a partial, and drops the switch if it's reverted", async () => {
    await loadedCodeMirror();
    const { rerender } = render(<Harness engine="textarea" dictating />);

    rerender(<Harness engine="codemirror" dictating />);
    expect(engineOf(composerInput())).toBe("textarea");
    rerender(<Harness engine="textarea" dictating />);
    rerender(<Harness engine="textarea" />);
    expect(engineOf(composerInput())).toBe("textarea");

    rerender(<Harness engine="codemirror" dictating />);
    rerender(<Harness engine="codemirror" />);
    expect(engineOf(composerInput())).toBe("codemirror");
  });

  it("keeps the standard input while the CodeMirror chunk hasn't loaded", () => {
    render(<Harness engine="codemirror" initial="draft" />);
    expect(engineOf(composerInput())).toBe("textarea");
    expect((composerInput() as HTMLTextAreaElement).disabled).toBe(false);
  });
});

describe("autofocus across an engine switch", () => {
  it.each(["textarea", "codemirror"] as const)(
    "focuses the first %s input when asked",
    async (engine) => {
      await loadedCodeMirror();
      render(<Harness engine={engine} autoFocus />);
      expect(document.activeElement).toBe(composerInput());
    },
  );

  it("keeps focus on another control when the deferred editor loads", async () => {
    let finishLoad: (module: typeof CodeMirrorModule) => void = () => {};
    const moduleLoaded = new Promise<typeof CodeMirrorModule>((resolve) => {
      finishLoad = resolve;
    });
    resetCodeMirrorComposerLoadForTests(() => moduleLoaded);
    render(<ComposerBesideButton engine="codemirror" autoFocus />);
    expect(document.activeElement).toBe(screen.getByRole("textbox"));

    const elsewhere = screen.getByRole("button", { name: "Elsewhere" });
    act(() => elsewhere.focus());
    act(() => loadCodeMirrorComposer());
    await act(async () => finishLoad(CodeMirrorModule));

    await waitFor(() => expect(engineOf(composerInput())).toBe("codemirror"));
    expect(document.activeElement).toBe(elsewhere);
  });

  it("keeps focus on another control when the editor hands back to the textarea", async () => {
    await loadedCodeMirror();
    const { rerender } = render(<ComposerBesideButton engine="codemirror" autoFocus />);
    expect(document.activeElement).toBe(composerInput());

    const elsewhere = screen.getByRole("button", { name: "Elsewhere" });
    act(() => elsewhere.focus());
    rerender(<ComposerBesideButton engine="textarea" autoFocus />);

    expect(engineOf(composerInput())).toBe("textarea");
    expect(document.activeElement).toBe(elsewhere);
  });
});

describe("switching with a reply quote's input active", () => {
  function QuoteHarness({
    engine,
    onActiveInputChange,
  }: {
    engine: ComposerInputEngine;
    onActiveInputChange: (target: ComposerTextTarget) => void;
  }) {
    const [quote, setQuote] = useState("quote reply text");
    const [tail, setTail] = useState("tail text");
    return (
      <>
        <button type="button">Palette</button>
        <ChatComposer
          engine={engine}
          onActiveInputChange={onActiveInputChange}
          keyboard={KEYBOARD}
          input={{
            "aria-label": "Message the agent",
            value: tail,
            onChange: (event) => setTail(event.currentTarget.value),
          }}
          slots={{
            inputPrefix: (
              <ComposerEngineInput
                keyboard={KEYBOARD}
                input={{
                  inputId: "quote-1",
                  "aria-label": "Reply text before quote 1",
                  value: quote,
                  onChange: (event) => setQuote(event.currentTarget.value),
                }}
              />
            ),
          }}
          actions={{ leading: null, trailing: null }}
        />
      </>
    );
  }

  const inputs = () => Array.from(document.querySelectorAll<HTMLElement>("[data-composer-input]"));
  const selectionOf = (element: HTMLElement) => {
    const target = composerTargetOf(element)!;
    return [target.selectionStart, target.selectionEnd];
  };

  it.each([
    ["textarea", "codemirror"],
    ["codemirror", "textarea"],
  ] as const)("keeps the quote active across %s → %s → back", async (first, second) => {
    await loadedCodeMirror();
    const active = vi.fn();
    const view = (engine: ComposerInputEngine) => (
      <QuoteHarness engine={engine} onActiveInputChange={active} />
    );
    const { rerender } = render(view(first));
    act(() => {
      const quote = composerTargetOf(inputs()[0])!;
      quote.focus();
      quote.setSelectionRange(4, 7);
    });
    act(() => screen.getByRole("button", { name: "Palette" }).focus());

    rerender(view(second));
    expect(selectionOf(inputs()[0])).toEqual([4, 7]);
    expect(active).toHaveBeenLastCalledWith(composerTargetOf(inputs()[0]));
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Palette" }));

    rerender(view(first));
    expect(selectionOf(inputs()[0])).toEqual([4, 7]);
    expect(active).toHaveBeenLastCalledWith(composerTargetOf(inputs()[0]));
    expect(composerTargetOf(inputs()[1])!.value).toBe("tail text");
  });
});

describe("switching while dictation streams", () => {
  it("switches as soon as a partial pins, even when the final matches it exactly", async () => {
    await loadedCodeMirror();
    const box: { dictation: ReturnType<typeof useDictationInsert> | null } = { dictation: null };
    function DictatingComposer({ engine }: { engine: ComposerInputEngine }) {
      const [value, setValue] = useState("");
      const dictation = useDictationInsert(value, setValue);
      box.dictation = dictation;
      return (
        <ChatComposer
          engine={engine}
          dictating={dictation.hasInterim}
          keyboard={KEYBOARD}
          input={{
            "aria-label": "Message the agent",
            value,
            onChange: (event) => setValue(event.currentTarget.value),
          }}
          actions={{ leading: null, trailing: null }}
        />
      );
    }
    const { rerender } = render(<DictatingComposer engine="textarea" />);
    act(() => box.dictation!.replaceInterim("same words"));

    rerender(<DictatingComposer engine="codemirror" />);
    expect(engineOf(composerInput())).toBe("textarea");

    // No draft change, so only the pinned region's own update can let the switch run.
    act(() => box.dictation!.appendFinal("same words"));

    expect(engineOf(composerInput())).toBe("codemirror");
    expect(composerTargetOf(composerInput())!.value).toBe("same words");
  });
});

describe("switching after the remembered input goes away", () => {
  function Quotes({
    engine,
    quoteIds,
    draftKey = "session-a",
    activeRef,
  }: {
    engine: ComposerInputEngine;
    quoteIds: string[];
    draftKey?: string;
    activeRef: { current: ComposerTextTarget | null };
  }) {
    const [tail, setTail] = useState("tail text");
    const dictation = useDictationInsert(tail, setTail, activeRef);
    dictationBox.current = dictation;
    return (
      <>
        <button type="button">Palette</button>
        <ChatComposer
          engine={engine}
          draftKey={draftKey}
          onActiveInputChange={(target) => {
            activeRef.current = target;
          }}
          keyboard={KEYBOARD}
          input={{
            "aria-label": "Message the agent",
            value: tail,
            onChange: (event) => setTail(event.currentTarget.value),
            onFocus: () => dictation.noteFocus(),
          }}
          slots={{
            inputPrefix: quoteIds.map((id) => (
              <ComposerEngineInput
                key={id}
                keyboard={KEYBOARD}
                input={{ inputId: id, "aria-label": `Reply ${id}`, defaultValue: `quote ${id}` }}
              />
            )),
          }}
          actions={{ leading: null, trailing: null }}
        />
      </>
    );
  }
  const dictationBox: { current: ReturnType<typeof useDictationInsert> | null } = {
    current: null,
  };
  const byLabel = (label: string) => composerTargetOf(screen.getByLabelText(label))!;

  it.each([
    ["textarea", "codemirror"],
    ["codemirror", "textarea"],
  ] as const)(
    "falls back to the tail once a removed quote was the active input (%s → %s)",
    async (first, second) => {
      await loadedCodeMirror();
      const activeRef: { current: ComposerTextTarget | null } = { current: null };
      const view = (engine: ComposerInputEngine, quoteIds: string[]) => (
        <Quotes engine={engine} quoteIds={quoteIds} activeRef={activeRef} />
      );
      const { rerender } = render(view(first, ["a", "b"]));
      act(() => {
        byLabel("Reply a").focus();
        byLabel("Reply a").setSelectionRange(2, 4);
      });
      act(() => screen.getByRole("button", { name: "Palette" }).focus());

      // The owner removes quote A; the next input in document order is quote B.
      rerender(view(first, ["b"]));
      rerender(view(second, ["b"]));

      const tail = byLabel("Message the agent");
      expect(activeRef.current).toBe(tail);
      expect(byLabel("Reply b").value).toBe("quote b");
      // Dictation now writes to the tail, with the tail's own text and caret.
      act(() => dictationBox.current!.appendFinal("spoken"));
      expect(byLabel("Message the agent").value).toBe("tail text spoken");
      expect(byLabel("Reply b").value).toBe("quote b");
    },
  );

  it("forgets the remembered quote when the draft's session changes", async () => {
    await loadedCodeMirror();
    const activeRef: { current: ComposerTextTarget | null } = { current: null };
    const view = (engine: ComposerInputEngine, draftKey: string) => (
      <Quotes engine={engine} quoteIds={["a"]} draftKey={draftKey} activeRef={activeRef} />
    );
    const { rerender } = render(view("textarea", "session-a"));
    act(() => byLabel("Reply a").focus());
    act(() => screen.getByRole("button", { name: "Palette" }).focus());

    rerender(view("textarea", "session-b"));
    rerender(view("codemirror", "session-b"));

    expect(activeRef.current).toBe(byLabel("Message the agent"));
  });
});

describe("a switch deferred when the session changes", () => {
  function Keyed({ engine, draftKey }: { engine: ComposerInputEngine; draftKey: string }) {
    const [value, setValue] = useState("draft");
    return (
      <ChatComposer
        engine={engine}
        draftKey={draftKey}
        keyboard={KEYBOARD}
        input={{
          "aria-label": "Message the agent",
          value,
          onChange: (event) => setValue(event.currentTarget.value),
        }}
        actions={{ leading: null, trailing: null }}
      />
    );
  }

  it.each([
    [
      "an IME composition",
      (input: HTMLElement) => fireEvent.compositionStart(input),
      (input: HTMLElement) => fireEvent.compositionEnd(input),
    ],
    [
      "a drag selection",
      (input: HTMLElement) => fireEvent.pointerDown(input),
      () => fireEvent.pointerUp(window),
    ],
  ])(
    "keeps waiting through a session change during %s, and runs once it ends",
    async (_name, start, end) => {
      await loadedCodeMirror();
      const { rerender } = render(<Keyed engine="textarea" draftKey="session-a" />);
      start(composerInput());

      rerender(<Keyed engine="codemirror" draftKey="session-a" />);
      expect(engineOf(composerInput())).toBe("textarea");

      // The requested engine stays the same; only the session changes.
      rerender(<Keyed engine="codemirror" draftKey="session-b" />);
      expect(engineOf(composerInput())).toBe("textarea");

      end(composerInput());
      expect(engineOf(composerInput())).toBe("codemirror");
    },
  );

  it("ignores a secondary-button press, which would never release a drag", async () => {
    await loadedCodeMirror();
    const { rerender } = render(<Keyed engine="textarea" draftKey="session-a" />);
    fireEvent.pointerDown(composerInput(), { button: 2 });

    rerender(<Keyed engine="codemirror" draftKey="session-a" />);

    expect(engineOf(composerInput())).toBe("codemirror");
  });

  it("runs a deferred switch once a drag selection is released in the same session", async () => {
    await loadedCodeMirror();
    const { rerender } = render(<Keyed engine="textarea" draftKey="session-a" />);
    fireEvent.pointerDown(composerInput());

    rerender(<Keyed engine="codemirror" draftKey="session-a" />);
    expect(engineOf(composerInput())).toBe("textarea");

    fireEvent.pointerUp(window);
    expect(engineOf(composerInput())).toBe("codemirror");
  });
});
