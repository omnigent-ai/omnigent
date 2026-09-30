import {
  createRef,
  useState,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ChatComposer,
  ComposerTextarea,
  ComposerSendButton,
  COMPOSER_LABELS_MIN_GAP_PX,
  MAX_HIGHLIGHTED_DRAFT_LENGTH,
} from "./ChatComposer";
import * as composerMarkdown from "@/lib/composerMarkdown";
import { MD_BOLD, MD_CODE, MD_FENCE, MD_ITALIC, MD_MARKER } from "@/lib/composerMarkdown";
import { highlightLayerRenderHooks } from "./ComposerHighlightLayer";

// A pass-through spy, so tests can count how often a draft is tokenized.
vi.mock("@/lib/composerMarkdown", async (importOriginal) => {
  const actual = await importOriginal<typeof composerMarkdown>();
  return { ...actual, tokenizeComposerMarkdown: vi.fn(actual.tokenizeComposerMarkdown) };
});

describe("ChatComposer", () => {
  it("keeps route-owned input and submit handlers on the shared surface", () => {
    const onChange = vi.fn();
    const onSubmit = vi.fn((event: FormEvent) => event.preventDefault());
    const inputRef = createRef<HTMLTextAreaElement>();
    render(
      <form onSubmit={onSubmit}>
        <ChatComposer
          data-testid="shared-composer"
          keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
          input={{ ref: inputRef, "aria-label": "Message", onChange }}
          actions={{
            leading: <span>Context controls</span>,
            trailing: <ComposerSendButton label="Send" />,
          }}
        />
      </form>,
    );
    expect(screen.getByTestId("shared-composer")).toHaveAttribute("data-composer-card");
    expect(inputRef.current).toBe(screen.getByRole("textbox"));
    expect(inputRef.current?.parentElement?.parentElement).toHaveClass(
      "relative",
      "overflow-hidden",
    );
    expect(screen.getByText("Context controls").parentElement?.parentElement).toHaveClass(
      "@container/composer-actions",
    );
    fireEvent.change(inputRef.current!, { target: { value: "Keep this draft" } });
    expect(onChange).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSubmit).toHaveBeenCalledOnce();
  });

  it("scopes responsive draft typography to the input area, not the toolbar", () => {
    render(
      <ChatComposer
        keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
        input={{ "aria-label": "Draft" }}
        actions={{ leading: <span>Composer actions</span>, trailing: null }}
      />,
    );
    const input = screen.getByRole("textbox");
    expect(input).toHaveClass("composer-input-text", "text-ui");
    expect(input.parentElement).toHaveClass("composer-input-text", "text-ui");
    expect(input).not.toHaveClass("text-[13px]", "leading-[20.8px]");
    expect(screen.getByText("Composer actions").closest(".composer-input-text")).toBeNull();
  });

  it("preserves interrupt and pending-creation states", () => {
    const { rerender } = render(<ComposerSendButton label="Interrupt" interrupt />);
    expect(screen.getByRole("button", { name: "Interrupt" })).toBeEnabled();
    rerender(<ComposerSendButton label="Starting session" busy disabled />);
    expect(screen.getByRole("button", { name: "Starting session" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Starting session" })).toHaveAttribute(
      "aria-busy",
      "true",
    );
  });

  it("uses the theme primary color for an enabled send action", () => {
    render(<ComposerSendButton label="Send" />);
    expect(screen.getByRole("button", { name: "Send" })).toHaveClass(
      "bg-primary",
      "text-primary-foreground",
      "disabled:bg-muted",
      "disabled:text-muted-foreground",
    );
  });

  it("uses the same button size in send and interrupt states", () => {
    const { rerender } = render(<ComposerSendButton label="Send" />);
    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toHaveClass("size-8");
    expect(send.className).toContain("md:size-7");
    rerender(<ComposerSendButton label="Interrupt" interrupt />);
    const interrupt = screen.getByRole("button", { name: "Interrupt" });
    expect(interrupt).toHaveClass("size-8");
    expect(interrupt.className).toContain("md:size-7");
  });

  it("places context, hints, attachments and controls around the same input", () => {
    const cardRef = createRef<HTMLDivElement>();
    render(
      <ChatComposer
        ref={cardRef}
        keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
        input={{ "aria-label": "Message", disabled: true }}
        slots={{
          beforeInput: <span>Quote</span>,
          inputHint: <span>Skills</span>,
          attachments: <span>Attachment</span>,
        }}
        actions={{
          leading: <span>Add</span>,
          trailing: <ComposerSendButton label="Send" disabled />,
        }}
      />,
    );
    const input = screen.getByRole("textbox");
    const field = input.parentElement!;
    const inputArea = field.parentElement!;
    expect(Array.from(inputArea.children)).toEqual([field, screen.getByText("Skills")]);
    expect(Array.from(cardRef.current!.children)).toEqual([
      screen.getByText("Quote"),
      inputArea,
      screen.getByText("Attachment"),
      screen.getByText("Add").parentElement!.parentElement,
    ]);
    expect(input).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("filters composition keys before invoking controller keyboard behavior", () => {
    const onKeyDown = vi.fn();
    render(<ComposerTextarea aria-label="Draft" onKeyDown={onKeyDown} />);
    const input = screen.getByRole("textbox");
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onKeyDown).not.toHaveBeenCalled();
    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    expect(onKeyDown).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onKeyDown).toHaveBeenCalledOnce();
  });

  it("shares send intent and touch newline precedence without submitting for the controller", () => {
    const onKeyDown = vi.fn();
    const props = {
      input: { "aria-label": "Draft", onKeyDown },
      actions: { leading: null, trailing: null },
    };
    const { rerender } = render(
      <ChatComposer
        {...props}
        keyboard={{ submitWithModEnter: true, preventsKeyboardSubmit: false }}
      />,
    );
    const input = screen.getByRole("textbox");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onKeyDown).toHaveBeenLastCalledWith(expect.anything(), {
      shouldSubmitFromKeyboard: false,
      shouldPreferSendOverCompletion: false,
      shouldSteerAllFromKeyboard: false,
    });
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });
    expect(onKeyDown).toHaveBeenLastCalledWith(expect.anything(), {
      shouldSubmitFromKeyboard: true,
      shouldPreferSendOverCompletion: true,
      shouldSteerAllFromKeyboard: false,
    });
    fireEvent.keyDown(input, { key: "Enter", ctrlKey: true, shiftKey: true });
    expect(onKeyDown).toHaveBeenLastCalledWith(expect.anything(), {
      shouldSubmitFromKeyboard: false,
      shouldPreferSendOverCompletion: false,
      shouldSteerAllFromKeyboard: true,
    });
    onKeyDown.mockClear();
    rerender(
      <ChatComposer
        {...props}
        keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
      />,
    );
    fireEvent.keyDown(input, { key: "Enter", metaKey: true });
    expect(onKeyDown).toHaveBeenLastCalledWith(expect.anything(), {
      shouldSubmitFromKeyboard: true,
      shouldPreferSendOverCompletion: false,
      shouldSteerAllFromKeyboard: true,
    });
    onKeyDown.mockClear();
    rerender(
      <ChatComposer
        {...props}
        keyboard={{ submitWithModEnter: true, preventsKeyboardSubmit: true }}
      />,
    );
    expect(fireEvent.keyDown(input, { key: "Enter", ctrlKey: true })).toBe(true);
    expect(onKeyDown).not.toHaveBeenCalled();
  });

  it.each([
    [false, false],
    [true, false],
    [false, true],
  ])(
    "inserts a line break at the caret on Alt+Enter (mod-enter: %s, touch: %s)",
    (submitWithModEnter, preventsKeyboardSubmit) => {
      const onKeyDown = vi.fn();
      const onChange = vi.fn();
      render(
        <ChatComposer
          keyboard={{ submitWithModEnter, preventsKeyboardSubmit }}
          input={{ "aria-label": "Draft", defaultValue: "firstsecond", onChange, onKeyDown }}
          actions={{ leading: null, trailing: null }}
        />,
      );
      const input = screen.getByRole<HTMLTextAreaElement>("textbox");
      input.setSelectionRange(5, 5);

      expect(fireEvent.keyDown(input, { key: "Enter", altKey: true })).toBe(false);
      expect(input).toHaveValue("first\nsecond");
      expect(input.selectionStart).toBe(6);
      expect(onChange).toHaveBeenCalledOnce();
      // Menus and send chords never see it, so it can't complete or submit.
      expect(onKeyDown).not.toHaveBeenCalled();
    },
  );

  it("leaves Alt chords with Ctrl/Cmd, or mid-composition, to the browser", () => {
    const onKeyDown = vi.fn();
    render(
      <ChatComposer
        keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
        input={{ "aria-label": "Draft", defaultValue: "draft", onKeyDown }}
        actions={{ leading: null, trailing: null }}
      />,
    );
    const input = screen.getByRole("textbox");
    expect(fireEvent.keyDown(input, { key: "Enter", altKey: true, ctrlKey: true })).toBe(true);
    expect(fireEvent.keyDown(input, { key: "Enter", altKey: true, metaKey: true })).toBe(true);
    fireEvent.compositionStart(input);
    expect(fireEvent.keyDown(input, { key: "Enter", altKey: true })).toBe(true);
    expect(input).toHaveValue("draft");
  });
});

describe("ChatComposer label collapse", () => {
  class StubResizeObserver {
    static callbacks: ResizeObserverCallback[] = [];
    constructor(callback: ResizeObserverCallback) {
      StubResizeObserver.callbacks.push(callback);
    }
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  const fireResize = () => {
    for (const callback of StubResizeObserver.callbacks) callback([], {} as ResizeObserver);
  };
  // Width of a group once its label is hidden and only the icon remains.
  const ICON_WIDTH = 40;
  const ROW_PADDING = 16;

  afterEach(() => {
    StubResizeObserver.callbacks = [];
    vi.unstubAllGlobals();
  });

  /**
   * jsdom does no layout, so stand in for it: the row is `layout.rowWidth`
   * wide with 8px side padding, and each group's natural width follows the
   * text inside it (10px per character) — except that, exactly like the real
   * CSS, a collapsed row hides the labels and leaves each group its icon width.
   */
  function renderMeasuredComposer(actions: { leading: ReactNode; trailing: ReactNode }) {
    vi.stubGlobal("ResizeObserver", StubResizeObserver);
    const layout = { rowWidth: 0 };
    const composer = (next: typeof actions) => (
      <ChatComposer
        keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
        input={{ "aria-label": "Message" }}
        actions={{ ...next, testId: "row", leadingTestId: "leading", trailingTestId: "trailing" }}
      />
    );
    const view = render(composer(actions));
    const row = screen.getByTestId("row");
    row.style.paddingLeft = "8px";
    row.style.paddingRight = "8px";
    Object.defineProperty(row, "clientWidth", { configurable: true, get: () => layout.rowWidth });
    for (const group of [screen.getByTestId("leading"), screen.getByTestId("trailing")]) {
      Object.defineProperty(group, "scrollWidth", {
        configurable: true,
        get: () =>
          row.dataset.labels === "collapsed" ? ICON_WIDTH : (group.textContent?.length ?? 0) * 10,
      });
    }
    return { row, layout, rerender: (next: typeof actions) => view.rerender(composer(next)) };
  }

  it("collapses the labels to icons once both groups stop fitting on one line with a gap", () => {
    const { row, layout } = renderMeasuredComposer({
      leading: <span>Bypass permissions</span>,
      trailing: <span>Fable 5.1 xHigh</span>,
    });
    const needed = 180 + 150 + COMPOSER_LABELS_MIN_GAP_PX;
    layout.rowWidth = needed + ROW_PADDING;
    fireResize();
    expect(row).not.toHaveAttribute("data-labels");
    layout.rowWidth = needed + ROW_PADDING - 1;
    fireResize();
    expect(row).toHaveAttribute("data-labels", "collapsed");
  });

  it("judges the expanded labels while collapsed, so icon-width room never brings them back", () => {
    const { row, layout } = renderMeasuredComposer({
      leading: <span>Bypass permissions</span>,
      trailing: <span>Fable 5.1 xHigh</span>,
    });
    layout.rowWidth = 200;
    fireResize();
    expect(row).toHaveAttribute("data-labels", "collapsed");
    // Plenty of room for two icons (2 × 40 + 24), none for the labels (354).
    layout.rowWidth = 300;
    fireResize();
    expect(row).toHaveAttribute("data-labels", "collapsed");
    layout.rowWidth = 180 + 150 + COMPOSER_LABELS_MIN_GAP_PX + ROW_PADDING;
    fireResize();
    expect(row).not.toHaveAttribute("data-labels");
  });

  it("re-measures when the controls inside the row change", async () => {
    const { row, layout, rerender } = renderMeasuredComposer({
      leading: <span>Manual</span>,
      trailing: <span>Sonnet 5</span>,
    });
    layout.rowWidth = 300;
    fireResize();
    expect(row).not.toHaveAttribute("data-labels");
    rerender({ leading: <span>Manual</span>, trailing: <span>Fable 5.1 (1M context) xHigh</span> });
    await waitFor(() => expect(row).toHaveAttribute("data-labels", "collapsed"));
    rerender({ leading: <span>Manual</span>, trailing: <span>Sonnet 5</span> });
    await waitFor(() => expect(row).not.toHaveAttribute("data-labels"));
  });
});

function nextFrame(): Promise<void> {
  return new Promise((resolve) => {
    requestAnimationFrame(() => resolve());
  });
}

describe("ComposerTextarea Markdown highlight layer", () => {
  function ControlledTextarea({
    initial = "",
    ...props
  }: { initial?: string } & Partial<Parameters<typeof ComposerTextarea>[0]>) {
    const [value, setValue] = useState(initial);
    return (
      <ComposerTextarea
        aria-label="Draft"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        {...props}
      />
    );
  }
  const layer = () => screen.queryByTestId("composer-highlight-overlay");
  const styledText = (flag: number) =>
    Array.from(layer()?.querySelectorAll<HTMLElement>("[data-md]") ?? [])
      .filter((span) => Number(span.dataset.md) & flag)
      .map((span) => span.textContent);

  it("styles Markdown live behind a transparent textarea", () => {
    render(<ControlledTextarea />);
    const input = screen.getByRole("textbox");
    expect(layer()).toBeNull();
    const draft = "_this_ **this** `this`\n```\nfenced\n```";
    fireEvent.change(input, { target: { value: draft } });

    expect(layer()).toHaveAttribute("aria-hidden", "true");
    expect(layer()).toHaveTextContent(draft, { normalizeWhitespace: false });
    expect(input).toHaveValue(draft);
    expect(input).toHaveClass("text-transparent", "caret-foreground", "selection:text-transparent");
    expect(styledText(MD_ITALIC)).toEqual(["this"]);
    expect(styledText(MD_BOLD)).toEqual(["this"]);
    expect(styledText(MD_CODE)).toEqual(["`", "this", "`"]);
    expect(styledText(MD_FENCE)).toEqual(["```", "\n", "fenced\n", "```"]);
    expect(styledText(MD_MARKER)).toEqual(["_", "_", "**", "**", "`", "`", "```", "```"]);
    // The layer is presentation only: the textarea stays the one accessible input.
    expect(screen.getAllByRole("textbox")).toEqual([input]);
    expect(layer()).toHaveClass("pointer-events-none", "select-none");
  });

  it("renders one block per draft line and keeps unchanged lines across edits", () => {
    render(<ControlledTextarea initial={"**a**\n\n_b_\nplain\n\n`c`"} />);
    // Lines are grouped into chunk blocks; each line is its own block inside one.
    const rows = () => Array.from(layer()!.querySelectorAll(":scope > div > div"));
    expect(rows().map((row) => row.textContent)).toEqual([
      "**a**\n",
      "\n",
      "_b_\n",
      "plain\n",
      "\n",
      "`c`",
    ]);
    const [first, , third] = rows();
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "**a**\n\n_b_\nplain\n\n`c` more" },
    });
    // Blocks above the edit keep their DOM nodes; only the edited line changes.
    expect(rows()[0]).toBe(first);
    expect(rows()[2]).toBe(third);
    expect(rows()[5].textContent).toBe("`c` more");
  });

  it("styles a draft up to the safety cap and renders one past it plain", () => {
    const atCap = `**bold** ${"x".repeat(MAX_HIGHLIGHTED_DRAFT_LENGTH - 9)}`;
    expect(atCap).toHaveLength(MAX_HIGHLIGHTED_DRAFT_LENGTH);
    const { unmount } = render(<ControlledTextarea initial={atCap} />);
    expect(styledText(MD_BOLD)).toEqual(["bold"]);
    unmount();
    const pastCap = `${atCap}x`;
    render(<ControlledTextarea initial={pastCap} />);
    expect(layer()).toBeNull();
    expect(screen.getByRole("textbox")).not.toHaveClass("text-transparent");
    expect(screen.getByRole("textbox")).toHaveValue(pastCap);
  });

  it("still tints a command token past the safety cap", () => {
    const draft = `/review **not parsed** ${"x".repeat(MAX_HIGHLIGHTED_DRAFT_LENGTH)}`;
    render(<ControlledTextarea initial={draft} accentRange={{ start: 0, end: 7 }} />);
    expect(layer()?.querySelector(".text-brand-accent")?.textContent).toBe("/review");
    expect(styledText(MD_BOLD)).toEqual([]);
    expect(layer()?.textContent).toBe(draft);
  });

  it("lays out CRLF and CR drafts as the textarea shows them (LF)", () => {
    const draft = "```\r\ncode\r\n```\r\n**after**\rend";
    render(<ControlledTextarea initial={draft} />);
    expect(layer()?.textContent).toBe("```\ncode\n```\n**after**\nend");
    expect(styledText(MD_BOLD)).toEqual(["after"]);
    expect(styledText(MD_FENCE).join("")).toBe("```\ncode\n```\n");
  });

  it("maps a command token's offsets past CRLF line breaks", () => {
    render(
      <ControlledTextarea initial={"\r\n\r\n/review _x_"} accentRange={{ start: 4, end: 11 }} />,
    );
    expect(layer()?.querySelector(".text-brand-accent")?.textContent).toBe("/review");
    expect(styledText(MD_ITALIC)).toEqual(["x"]);
  });

  it("paints the textarea's own glyphs when nothing is styled", () => {
    render(<ControlledTextarea initial="snake_case_name, file_name.py and 2 * 3" />);
    expect(layer()).toBeNull();
    expect(screen.getByRole("textbox")).not.toHaveClass("text-transparent");
  });

  it("uses only width-neutral styles, in the textarea's typography", () => {
    render(<ControlledTextarea initial="_a_ **b** `c`" />);
    expect(layer()).toHaveClass("composer-input-text", "text-ui", "whitespace-pre-wrap");
    for (const span of layer()!.querySelectorAll("span")) {
      expect(span.className).not.toMatch(/\b(italic|font-|p[xytrbl]?-|m[xytrbl]?-|border)/);
    }
  });

  it("tints the accent range as a command token", () => {
    render(<ControlledTextarea initial="/review the _diff_" accentRange={{ start: 0, end: 7 }} />);
    expect(layer()?.querySelector(".text-brand-accent")?.textContent).toBe("/review");
    expect(styledText(MD_ITALIC)).toEqual(["diff"]);
  });

  it("steps aside during IME composition so the native composition text shows", () => {
    render(<ControlledTextarea initial="**bold** " />);
    const input = screen.getByRole("textbox");
    expect(layer()).not.toBeNull();
    fireEvent.compositionStart(input);
    expect(layer()).toBeNull();
    expect(input).not.toHaveClass("text-transparent");
    fireEvent.change(input, { target: { value: "**bold** にほんご" } });
    fireEvent.compositionEnd(input);
    expect(layer()).toHaveTextContent("**bold** にほんご");
    expect(input).toHaveClass("text-transparent");
  });

  it("follows the textarea's scroll offset", () => {
    const onScroll = vi.fn();
    render(<ControlledTextarea initial={"_a_\n".repeat(40)} onScroll={onScroll} />);
    const input = screen.getByRole("textbox");
    input.scrollTop = 120;
    fireEvent.scroll(input);
    expect(layer()!.scrollTop).toBe(120);
    expect(onScroll).toHaveBeenCalledOnce();
  });

  it("never scroll-anchors, and re-copies the textarea's scroll after an edit", async () => {
    render(<ControlledTextarea initial={"_a_\n".repeat(40)} />);
    const input = screen.getByRole("textbox");
    expect(layer()).toHaveClass("[overflow-anchor:none]");
    // A scroll change that no scroll event reported, as after an edit above it.
    input.scrollTop = 120;
    layer()!.scrollTop = 0;
    fireEvent.change(input, { target: { value: `**new** text\n${"_a_\n".repeat(40)}` } });
    await nextFrame();
    await nextFrame();
    expect(layer()!.scrollTop).toBe(120);
  });

  it("holds a trailing newline's empty line open", () => {
    render(<ControlledTextarea initial={"_a_\n"} />);
    expect(layer()).toHaveAttribute("data-trailing-newline");
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "_a_" } });
    expect(layer()).not.toHaveAttribute("data-trailing-newline");
  });

  it("forwards object and callback refs to the textarea", () => {
    const objectRef = createRef<HTMLTextAreaElement>();
    const callbackRef = vi.fn();
    const { unmount } = render(<ComposerTextarea aria-label="A" ref={objectRef} />);
    expect(objectRef.current).toBe(screen.getByRole("textbox"));
    unmount();
    render(<ComposerTextarea aria-label="B" ref={callbackRef} />);
    expect(callbackRef).toHaveBeenCalledWith(screen.getByRole("textbox"));
  });

  const markers = () =>
    Array.from(layer()?.querySelectorAll<HTMLElement>("[data-md]") ?? []).filter(
      (span) => Number(span.dataset.md) & MD_MARKER,
    );
  const revealedMarkers = () =>
    markers()
      .filter((span) => span.hasAttribute("data-revealed"))
      .map((span) => span.textContent);
  function moveCaret(input: HTMLElement, start: number, end = start) {
    (input as HTMLTextAreaElement).setSelectionRange(start, end);
    fireEvent.select(input);
  }

  it("hides markers, keeping their width, until the caret edits their span", () => {
    const draft = "say _hi_ and **bold** then `code`";
    render(<ControlledTextarea initial={draft} />);
    const input = screen.getByRole("textbox");
    // Transparent, never removed: the layer still holds every character.
    expect(layer()?.textContent).toBe(draft);
    expect(markers().map((span) => span.textContent)).toEqual(["_", "_", "**", "**", "`", "`"]);
    for (const span of markers()) expect(span).toHaveClass("text-transparent");
    expect(revealedMarkers()).toEqual([]);

    act(() => input.focus());
    moveCaret(input, draft.indexOf("hi") + 1);
    expect(revealedMarkers()).toEqual(["_", "_"]);
    for (const span of markers().filter((m) => m.hasAttribute("data-revealed"))) {
      expect(span).toHaveClass("text-muted-foreground");
      expect(span).not.toHaveClass("text-transparent");
    }
    // Right beside a span's closing marker still counts as editing it.
    moveCaret(input, draft.indexOf("**bold**") + 8);
    expect(revealedMarkers()).toEqual(["**", "**"]);
    moveCaret(input, draft.length);
    expect(revealedMarkers()).toEqual(["`", "`"]);
    moveCaret(input, 1);
    expect(revealedMarkers()).toEqual([]);
  });

  it("reveals every span a selection touches, and hides them all on blur", () => {
    const draft = "say _hi_ and **bold** then `code`";
    render(<ControlledTextarea initial={draft} />);
    const input = screen.getByRole("textbox");
    act(() => input.focus());
    moveCaret(input, draft.indexOf("hi"), draft.indexOf("old"));
    expect(revealedMarkers()).toEqual(["_", "_", "**", "**"]);
    fireEvent.keyUp(input);
    expect(revealedMarkers()).toEqual(["_", "_", "**", "**"]);
    act(() => input.blur());
    expect(revealedMarkers()).toEqual([]);
  });

  it("tracks the caret as the user types", () => {
    render(<ControlledTextarea />);
    const input = screen.getByRole("textbox") as HTMLTextAreaElement;
    act(() => input.focus());
    fireEvent.input(input, { target: { value: "a **b**", selectionStart: 7, selectionEnd: 7 } });
    expect(revealedMarkers()).toEqual(["**", "**"]);
    fireEvent.input(input, { target: { value: "a **b** c", selectionStart: 9, selectionEnd: 9 } });
    expect(revealedMarkers()).toEqual([]);
  });

  it("re-renders only the lines whose markers change", () => {
    render(<ControlledTextarea initial={"**a**\nplain\n_b_"} />);
    const input = screen.getByRole("textbox");
    const rows = () => Array.from(layer()!.querySelectorAll(":scope > div > div"));
    const [first, second, third] = rows();
    act(() => input.focus());
    moveCaret(input, 2);
    expect(rows()[0]).toBe(first);
    expect(rows()[1]).toBe(second);
    expect(rows()[2]).toBe(third);
    expect(revealedMarkers()).toEqual(["**", "**"]);
  });

  it("paints inline code as one pill around its backticks", () => {
    render(<ControlledTextarea initial="run `npm test` now" />);
    const pills = layer()!.querySelectorAll("[data-code-pill]");
    expect(pills).toHaveLength(1);
    expect(pills[0].textContent).toBe("`npm test`");
    expect(pills[0]).toHaveClass("[background-color:var(--code-bg)]");
    expect(pills[0].className).toMatch(/\brounded/);
    // The pill wraps the runs; the runs themselves carry no background.
    for (const span of pills[0].querySelectorAll("[data-md]"))
      expect(span.className).not.toMatch(/background-color/);
  });

  it("draws a fenced block as one box and reveals its fences from inside", () => {
    const draft = "intro\n```ts\nconst a = 1;\nconst b = 2;\n```\nafter";
    render(<ControlledTextarea initial={draft} />);
    const input = screen.getByRole("textbox");
    const blocks = layer()!.querySelectorAll("[data-code-block]");
    expect(blocks).toHaveLength(1);
    expect(blocks[0].textContent).toBe("```ts\nconst a = 1;\nconst b = 2;\n```\n");
    expect(blocks[0]).toHaveClass("[background-color:var(--code-bg)]", "rounded-md");
    expect(blocks[0]).not.toHaveAttribute("data-trailing-newline");
    for (const span of blocks[0].querySelectorAll("[data-md]"))
      expect(span.className).not.toMatch(/background-color/);
    expect(layer()?.textContent).toBe(draft);

    // Only the backticks are markers; the info string stays visible, dimmed.
    const info = Array.from(blocks[0].querySelectorAll<HTMLElement>("[data-md]")).find(
      (span) => span.textContent === "ts",
    );
    expect(info).toHaveClass("text-muted-foreground");
    expect(info).not.toHaveClass("text-transparent");
    expect(markers().map((span) => span.textContent)).toEqual(["```", "```"]);

    act(() => input.focus());
    moveCaret(input, draft.indexOf("b = 2"));
    expect(revealedMarkers()).toEqual(["```", "```"]);
    moveCaret(input, draft.indexOf("```"));
    expect(revealedMarkers()).toEqual(["```", "```"]);
    moveCaret(input, draft.indexOf("after"));
    expect(revealedMarkers()).toEqual([]);
    expect(info).toHaveClass("text-muted-foreground");
  });

  it("boxes an unclosed fence through the draft's last line", () => {
    render(<ControlledTextarea initial={"intro\n```\nstill typing\n"} />);
    const input = screen.getByRole("textbox");
    const block = layer()!.querySelector("[data-code-block]");
    expect(block?.textContent).toBe("```\nstill typing\n");
    // The box, not the layer, holds the empty line after the final newline.
    expect(block).toHaveAttribute("data-trailing-newline");
    expect(layer()).not.toHaveAttribute("data-trailing-newline");
    // Unclosed, the fence has no token: its backticks show dimmed wherever the caret is.
    const backticks = block!.querySelector<HTMLElement>("[data-md]")!;
    expect(backticks.textContent).toBe("```");
    act(() => input.focus());
    for (const caret of [(input as HTMLTextAreaElement).value.length, 2]) {
      moveCaret(input, caret);
      expect(backticks).toHaveClass("text-muted-foreground");
      expect(backticks).not.toHaveClass("text-transparent");
      expect(markers()).toEqual([]);
    }
  });

  it("reveals only the adjacent token the caret touches", () => {
    render(<ControlledTextarea initial="say _a_**b** end" />);
    const input = screen.getByRole("textbox");
    act(() => input.focus());
    moveCaret(input, 5);
    expect(revealedMarkers()).toEqual(["_", "_"]);
    moveCaret(input, 9);
    expect(revealedMarkers()).toEqual(["**", "**"]);
    moveCaret(input, 7);
    expect(revealedMarkers()).toEqual(["_", "_", "**", "**"]);
  });

  /** The text of each row whose markers are revealed, in draft order. */
  const revealedRows = () =>
    Array.from(
      new Set(
        Array.from(layer()!.querySelectorAll("[data-revealed]"), (span) =>
          span.closest("div")!.textContent!.trimEnd(),
        ),
      ),
    );

  it("re-reveals by absolute offset when unchanged rows shift below an edit", () => {
    const lines = Array.from({ length: 400 }, (_, i) => `line${String(i).padStart(3, "0")} **b**`);
    const draft = lines.join("\n");
    render(<ControlledTextarea initial={draft} />);
    const input = screen.getByRole("textbox") as HTMLTextAreaElement;
    act(() => input.focus());
    // Each row is 14 characters with its newline: 2811 is line200's closing `**`.
    moveCaret(input, 2811);
    expect(revealedRows()).toEqual(["line200 **b**"]);
    // A same-length line lands above: 2811 is now line199's closing `**`, at the
    // same absolute marker offsets line200's had, while every row is unchanged.
    fireEvent.change(input, { target: { value: `prefix! **b**\n${draft}` } });
    moveCaret(input, 2811);
    expect(revealedRows()).toEqual(["line199 **b**"]);
    expect(revealedMarkers()).toEqual(["**", "**"]);
  });

  it("hides markers inside a command token after blur, tinting only its other characters", () => {
    render(<ControlledTextarea initial="/x:_a_ end" accentRange={{ start: 0, end: 6 }} />);
    const input = screen.getByRole("textbox");
    const underscores = () => markers().filter((span) => span.textContent === "_");
    const accented = () =>
      Array.from(layer()!.querySelectorAll(".text-brand-accent"), (span) => span.textContent);
    expect(underscores()).toHaveLength(2);
    // Unfocused: the markers are hidden, and only the command's own characters tint.
    for (const span of underscores()) {
      expect(span).toHaveClass("text-transparent");
      expect(span).not.toHaveClass("text-brand-accent");
    }
    expect(accented().join("")).toBe("/x:a");
    // Focused inside the token: its markers show dimmed, still not tinted.
    act(() => input.focus());
    moveCaret(input, 5);
    for (const span of underscores()) {
      expect(span).toHaveAttribute("data-revealed");
      expect(span).toHaveClass("text-muted-foreground");
      expect(span).not.toHaveClass("text-brand-accent", "text-transparent");
    }
    expect(accented().join("")).toBe("/x:a");
    // Blurred again: hidden again.
    act(() => input.blur());
    for (const span of underscores()) {
      expect(span).toHaveClass("text-transparent");
      expect(span).not.toHaveAttribute("data-revealed");
    }
  });

  it("re-renders only the touched rows, and never re-tokenizes, when the caret moves", () => {
    const lines = Array.from({ length: 400 }, (_, i) => `line ${i} with **bold** and _em_`);
    render(<ControlledTextarea initial={lines.join("\n")} />);
    const input = screen.getByRole("textbox") as HTMLTextAreaElement;
    act(() => input.focus());
    const lineAt = (i: number) => lines.slice(0, i).join("\n").length + (i > 0 ? 1 : 0);
    moveCaret(input, lineAt(200) + 1);
    const tokenize = vi.mocked(composerMarkdown.tokenizeComposerMarkdown);
    tokenize.mockClear();
    let rows = 0;
    let chunks = 0;
    highlightLayerRenderHooks.onRow = () => rows++;
    highlightLayerRenderHooks.onChunk = () => chunks++;
    try {
      // Into line 200's bold: that one row gains its markers.
      moveCaret(input, lineAt(200) + lines[200].indexOf("bold"));
      expect([rows, chunks]).toEqual([1, 1]);
      // Across to line 201's bold: line 200 loses its markers, line 201 gains them.
      rows = 0;
      chunks = 0;
      moveCaret(input, lineAt(201) + lines[201].indexOf("bold"));
      expect(rows).toBe(2);
      expect(chunks).toBeLessThanOrEqual(2);
      // A caret move re-tokenizes nothing.
      expect(tokenize).not.toHaveBeenCalled();
      // An edit tokenizes once and re-renders the chunk around the edited row,
      // never the 400 rows: rows shifted below it reveal nothing, so they skip.
      // (A new line id can fall on a chunk boundary and regroup that chunk, so
      // the bound is two 64-row chunks rather than one row.)
      rows = 0;
      chunks = 0;
      const next = input.value.slice(0, lineAt(10)) + "x" + input.value.slice(lineAt(10));
      fireEvent.change(input, { target: { value: next } });
      moveCaret(input, lineAt(10) + 1);
      expect(tokenize).toHaveBeenCalledTimes(1);
      expect(rows).toBeGreaterThan(0);
      expect(rows).toBeLessThanOrEqual(128);
      expect(chunks).toBeLessThanOrEqual(4);
    } finally {
      highlightLayerRenderHooks.onRow = undefined;
      highlightLayerRenderHooks.onChunk = undefined;
    }
  });

  it("touches only the rows around the caret when it moves in a long draft", async () => {
    const lines = Array.from({ length: 400 }, (_, i) => `line ${i} with **bold** and _em_`);
    render(<ControlledTextarea initial={lines.join("\n")} />);
    const input = screen.getByRole("textbox") as HTMLTextAreaElement;
    act(() => input.focus());
    const lineAt = (i: number) => lines.slice(0, i).join("\n").length + (i > 0 ? 1 : 0);
    moveCaret(input, lineAt(200) + 1);
    const rows = Array.from(layer()!.querySelectorAll<HTMLElement>(":scope div > div")).filter(
      (row) => !row.hasAttribute("data-code-block"),
    );
    const before = new Set(rows);
    const touched = new Set<Node>();
    const observer = new MutationObserver((records) => {
      for (const record of records) {
        const row = rows.find((candidate) => candidate.contains(record.target));
        if (row) touched.add(row);
      }
    });
    observer.observe(layer()!, { subtree: true, childList: true, attributes: true });
    // Into line 200's bold, then into line 201's bold: two rows' markers change.
    moveCaret(input, lineAt(200) + lines[200].indexOf("bold"));
    moveCaret(input, lineAt(201) + lines[201].indexOf("bold"));
    await Promise.resolve();
    observer.disconnect();
    expect(revealedMarkers()).toEqual(["**", "**"]);
    expect(touched.size).toBeGreaterThan(0);
    expect(touched.size).toBeLessThanOrEqual(2);
    // No row was replaced, only updated in place.
    for (const row of rows) expect(before.has(row) && row.isConnected).toBe(true);
  });

  it("leaves IME composition to the textarea and reads the caret once it commits", () => {
    render(<ControlledTextarea initial="**bold** " />);
    const input = screen.getByRole("textbox");
    act(() => input.focus());
    moveCaret(input, 9);
    expect(revealedMarkers()).toEqual([]);
    fireEvent.compositionStart(input);
    moveCaret(input, 3);
    expect(layer()).toBeNull();
    fireEvent.change(input, { target: { value: "**bold** にほんご" } });
    moveCaret(input, 3);
    fireEvent.compositionEnd(input);
    expect(layer()).toHaveTextContent("**bold** にほんご");
    expect(revealedMarkers()).toEqual(["**", "**"]);
  });

  it("dims with the disabled textarea", () => {
    render(<ControlledTextarea initial="**x**" disabled />);
    expect(layer()).toHaveClass("opacity-60");
  });
});

const DESKTOP_KEYBOARD = { submitWithModEnter: false, preventsKeyboardSubmit: false };

describe("ComposerTextarea Markdown editing and new styles", () => {
  function Controlled({
    initial = "",
    onKeyDown,
    keyboard = DESKTOP_KEYBOARD,
  }: {
    initial?: string;
    onKeyDown?: (event: ReactKeyboardEvent<HTMLTextAreaElement>, intent: unknown) => void;
    keyboard?: { submitWithModEnter: boolean; preventsKeyboardSubmit: boolean };
  }) {
    const [value, setValue] = useState(initial);
    return (
      <ChatComposer
        keyboard={keyboard}
        input={{
          "aria-label": "Draft",
          value,
          onChange: (event) => setValue(event.target.value),
          onKeyDown,
        }}
        actions={{ leading: null, trailing: null }}
      />
    );
  }
  const input = () => screen.getByRole("textbox") as HTMLTextAreaElement;
  const layer = () => screen.queryByTestId("composer-highlight-overlay");

  /**
   * Type one character the way a keystroke arrives: a cancelable beforeinput,
   * then (unless cancelled) the insertion and an insertText input.
   */
  async function type(
    char: string,
    inputType = "insertText",
    isComposing = false,
    { beforeInput = true } = {},
  ) {
    const textarea = input();
    const prevented =
      beforeInput &&
      !textarea.dispatchEvent(
        new InputEvent("beforeinput", {
          bubbles: true,
          cancelable: true,
          inputType,
          data: char,
          isComposing,
        }),
      );
    if (prevented) return;
    const at = textarea.selectionStart;
    const value = textarea.value.slice(0, at) + char + textarea.value.slice(textarea.selectionEnd);
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(
      textarea,
      value,
    );
    textarea.setSelectionRange(at + char.length, at + char.length);
    act(() => {
      textarea.dispatchEvent(
        new InputEvent("input", { bubbles: true, inputType, data: char, isComposing }),
      );
    });
    // The edit lands in a microtask after the keystroke.
    await act(async () => {
      await Promise.resolve();
    });
  }

  /** Backspace as it arrives: a cancelable beforeinput, then the deletion. */
  function backspace() {
    const textarea = input();
    const prevented = !textarea.dispatchEvent(
      new InputEvent("beforeinput", {
        bubbles: true,
        cancelable: true,
        inputType: "deleteContentBackward",
      }),
    );
    if (prevented) return;
    const at = textarea.selectionStart;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(
      textarea,
      textarea.value.slice(0, at - 1) + textarea.value.slice(at),
    );
    textarea.setSelectionRange(at - 1, at - 1);
    act(() => {
      textarea.dispatchEvent(
        new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward" }),
      );
    });
  }

  /** The draft with `|` at the caret. */
  const shown = () => {
    const { value, selectionStart } = input();
    return value.slice(0, selectionStart) + "|" + value.slice(selectionStart);
  };

  function place(text: string) {
    render(<Controlled initial={text} />);
    act(() => input().focus());
    input().setSelectionRange(text.length, text.length);
  }

  it("tidies a stray space when the closing marker is typed", async () => {
    place("** Is this Bold?!*");
    await type("*");
    expect(input().value).toBe("**Is this Bold?!**");
    expect(input().selectionStart).toBe(input().value.length);
  });

  it("tidies before the closer too, and for underscores", async () => {
    place("__bold _");
    await type("_");
    expect(input().value).toBe("__bold__");
  });

  it("never tidies a paste, a composition, a guard case or a programmatic change", async () => {
    place("** pasted*");
    await type("*", "insertFromPaste");
    expect(input().value).toBe("** pasted**");
    cleanup();
    place("** composing*");
    await type("*", "insertText", true);
    expect(input().value).toBe("** composing**");
    cleanup();
    place("2 ** 3 *");
    await type("*");
    expect(input().value).toBe("2 ** 3 **");
    cleanup();
    // History recall, draft restore and dictation set the value: no typed input.
    render(<Controlled initial="** restored**" />);
    expect(input().value).toBe("** restored**");
  });

  it("pairs a typed opener, and steps over its closer", async () => {
    place("say ");
    await type("*");
    expect(shown()).toBe("say *|*");
    await type("*");
    expect(shown()).toBe("say **|**");
    await type("b");
    await type("o");
    await type("l");
    await type("d");
    expect(shown()).toBe("say **bold|**");
    // One `*` ends the bold: it steps over the whole closer.
    await type("*");
    expect(shown()).toBe("say **bold**|");
    await type("`");
    expect(shown()).toBe("say **bold**`|");
  });

  it("pairs input that comes without a beforeinput (execCommand)", async () => {
    place("say ");
    const typeBare = (char: string) => type(char, "insertText", false, { beforeInput: false });
    await typeBare("*");
    await typeBare("*");
    expect(shown()).toBe("say **|**");
    await typeBare("b");
    await typeBare("*");
    expect(shown()).toBe("say **b**|");
    // The closer's second `*`, typed in full, lands and is taken back out.
    await typeBare("*");
    expect(shown()).toBe("say **b**|");
  });

  it("drops a pair with Backspace, or its closer with a space", async () => {
    place("");
    await type("*");
    backspace();
    await act(async () => {
      await Promise.resolve();
    });
    expect(shown()).toBe("|");
    await type("*");
    await type(" ");
    expect(shown()).toBe("* |");
  });

  it("wraps a selection in the typed marker", async () => {
    place("say word now");
    input().setSelectionRange(4, 8);
    await type("*");
    expect(input().value).toBe("say *word* now");
    expect([input().selectionStart, input().selectionEnd]).toEqual([5, 9]);
  });

  it("never pairs a paste, a composition or after a restore", async () => {
    place("say ");
    await type("*", "insertFromPaste");
    expect(shown()).toBe("say *|");
    cleanup();
    place("say ");
    await type("*", "insertText", true);
    expect(shown()).toBe("say *|");
    cleanup();
    // A draft restored (set without typing) holds no inserted closers: its `*`
    // is the user's own text, so typing `*` before it inserts.
    render(<Controlled initial="say *hi*" />);
    act(() => input().focus());
    input().setSelectionRange(7, 7);
    await type("*");
    expect(shown()).toBe("say *hi*|*");
  });

  it("completes an open ** typed by hand", async () => {
    place("**text");
    await type("*");
    expect(shown()).toBe("**text**|");
    await type("*");
    expect(shown()).toBe("**text**|");
  });

  it("turns ``` typed on an empty line into a code block", async () => {
    place("intro\n``");
    await type("`");
    expect(input().value).toBe("intro\n```\n\n```");
    expect(input().selectionStart).toBe("intro\n```\n".length);
  });

  it("pairs a backtick on an empty line, and three typed make the code block", async () => {
    place("intro\n");
    await type("`");
    expect(shown()).toBe("intro\n`|`");
    await type("`");
    expect(shown()).toBe("intro\n``|``");
    await type("`");
    expect(shown()).toBe("intro\n```\n|\n```");
    cleanup();
    // Or inline code on its own line.
    place("");
    // Keystrokes land one at a time.
    /* oxlint-disable no-await-in-loop */
    for (const char of "`code`") await type(char);
    /* oxlint-enable no-await-in-loop */
    expect(shown()).toBe("`code`|");
  });

  it("makes Enter a newline inside a code block, and Cmd/Ctrl+Enter still sends", () => {
    const onKeyDown = vi.fn();
    render(<Controlled initial={"```\ncode\n```"} onKeyDown={onKeyDown} />);
    act(() => input().focus());
    input().setSelectionRange(6, 6);
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(onKeyDown.mock.calls[0][1]).toMatchObject({ shouldSubmitFromKeyboard: false });
    fireEvent.keyDown(input(), { key: "Enter", ctrlKey: true });
    expect(onKeyDown.mock.calls[1][1]).toMatchObject({ shouldSubmitFromKeyboard: true });
    // Outside a block, Enter sends as before.
    input().setSelectionRange(input().value.length + 0, input().value.length);
    cleanup();
    const send = vi.fn();
    render(<Controlled initial="plain" onKeyDown={send} />);
    input().setSelectionRange(5, 5);
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(send.mock.calls[0][1]).toMatchObject({ shouldSubmitFromKeyboard: true });
  });

  it("keeps a fence in a quoted list item code: Enter adds its line, the tidy stays off", async () => {
    const onKeyDown = vi.fn();
    const draft = "> - ```\n>   ** not bold**\n>   ```";
    render(<Controlled initial={draft} onKeyDown={onKeyDown} />);
    act(() => input().focus());
    const at = draft.indexOf("not") + 3;
    input().setSelectionRange(at, at);
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(onKeyDown.mock.calls[0][1]).toMatchObject({ shouldSubmitFromKeyboard: false });
    expect(input().value).toBe("> - ```\n>   ** not\n>    bold**\n>   ```");
    expect(input().selectionStart).toBe(at + 5);
    cleanup();
    // Retyping the closing `*` inside the block leaves the code as typed.
    render(<Controlled initial={"> - ```\n>   ** not bold*\n>   ```"} />);
    act(() => input().focus());
    const end = "> - ```\n>   ** not bold*".length;
    input().setSelectionRange(end, end);
    await type("*");
    expect(input().value).toBe(draft);
  });

  it("exits a code block from its empty last line, unless a menu claims Enter", () => {
    render(<Controlled initial={"```\ncode\n\n```"} />);
    act(() => input().focus());
    input().setSelectionRange(9, 9);
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(input().value).toBe("```\ncode\n```\n");
    expect(input().selectionStart).toBe(input().value.length);
    cleanup();
    const menu = vi.fn((event: ReactKeyboardEvent) => event.preventDefault());
    render(<Controlled initial={"```\ncode\n\n```"} onKeyDown={menu} />);
    input().setSelectionRange(9, 9);
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(input().value).toBe("```\ncode\n\n```");
  });

  it("exits on a touch keyboard too, where Enter never sends", () => {
    render(
      <Controlled
        initial={"```\ncode\n"}
        keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: true }}
      />,
    );
    act(() => input().focus());
    input().setSelectionRange(9, 9);
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(input().value).toBe("```\ncode\n```\n");
  });

  it("makes Alt+Enter a line in a code block or quote: never an exit, a send or a menu key", () => {
    const onKeyDown = vi.fn();
    // A quoted code block's new line keeps the quote, on its empty last line too.
    const quotedFence = "> ```\n> code\n> \n> ```";
    render(<Controlled initial={quotedFence} onKeyDown={onKeyDown} />);
    act(() => input().focus());
    const empty = "> ```\n> code\n> ".length;
    input().setSelectionRange(empty, empty);
    expect(fireEvent.keyDown(input(), { key: "Enter", altKey: true })).toBe(false);
    expect(input().value).toBe("> ```\n> code\n> \n> \n> ```");
    expect(input().selectionStart).toBe(empty + 3);
    cleanup();
    // A plain code block's empty last line gets a bare line, no exit.
    render(<Controlled initial={"```\ncode\n\n```"} onKeyDown={onKeyDown} />);
    act(() => input().focus());
    input().setSelectionRange(9, 9);
    fireEvent.keyDown(input(), { key: "Enter", altKey: true });
    expect(input().value).toBe("```\ncode\n\n\n```");
    expect(input().selectionStart).toBe(10);
    cleanup();
    // In a quote outside any block, Alt+Enter is a bare line break, as Shift+Enter.
    render(<Controlled initial="> quoted text" onKeyDown={onKeyDown} />);
    act(() => input().focus());
    input().setSelectionRange(8, 8);
    fireEvent.keyDown(input(), { key: "Enter", altKey: true });
    expect(input().value).toBe("> quoted\n text");
    expect(input().selectionStart).toBe(9);
    expect(onKeyDown).not.toHaveBeenCalled();
  });

  it("handles ArrowDown and Enter from the current parse, never re-tokenizing", () => {
    const lines = Array.from({ length: 300 }, (_, i) => `line ${i} with **bold**`);
    const draft = `${lines.join("\n")}\n\`\`\`\ncode one\ncode two\n\`\`\``;
    render(<Controlled initial={draft} />);
    act(() => input().focus());
    const tokenize = vi.mocked(composerMarkdown.tokenizeComposerMarkdown);
    tokenize.mockClear();
    // Plain text, a code block's inner line, and Enter inside the block.
    input().setSelectionRange(5, 5);
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    const inner = draft.indexOf("code one") + 2;
    input().setSelectionRange(inner, inner);
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(tokenize).not.toHaveBeenCalled();
    // An exit edits the draft: only the re-render parses, and only the new text.
    const last = draft.indexOf("code two") + 2;
    input().setSelectionRange(last, last);
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().value).toBe(`${draft}\n`);
    expect(tokenize.mock.calls.map(([text]) => text)).toEqual([`${draft}\n`]);
  });

  it("moves past a code block with ArrowDown on its last line", () => {
    render(<Controlled initial={"```\ncode\n```"} />);
    act(() => input().focus());
    input().setSelectionRange(6, 6);
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(input().value).toBe("```\ncode\n```\n");
    expect(input().selectionStart).toBe(input().value.length);
  });

  it("styles strikethrough, links and quotes with paint-only classes", () => {
    render(<Controlled initial={"~~gone~~ [docs](https://x.test) > not\n> quoted"} />);
    const span = (text: string) =>
      Array.from(layer()!.querySelectorAll<HTMLElement>("[data-md]")).find(
        (candidate) => candidate.textContent === text,
      )!;
    expect(span("gone")).toHaveClass("md-deco", "md-del");
    expect(span("docs")).toHaveClass("md-deco", "md-a", "text-primary");
    // The url stays readable: dimmed, never transparent.
    expect(span("https://x.test")).toHaveClass("text-muted-foreground");
    expect(span("https://x.test")).not.toHaveClass("text-transparent");
    for (const marker of ["~~", "[", "](", ")"])
      expect(span(marker)).toHaveClass("text-transparent");
    const quoteRow = span(" quoted").closest("div")!;
    expect(quoteRow).toHaveClass("composer-quote");
    expect(span(" quoted")).toHaveClass("text-muted-foreground");
    expect(span("gone").closest("div")).not.toHaveClass("composer-quote");
  });

  it("joins consecutive quoted lines into one block quote, lazy lines included", () => {
    render(<Controlled initial={"intro\n> first\n> middle\nlazy line\n\n> alone\n\nafter"} />);
    const rows = () => Array.from(layer()!.querySelectorAll<HTMLElement>(".composer-quote"));
    expect(rows().map((row) => [row.textContent, row.dataset.quote])).toEqual([
      ["> first\n", "first"],
      ["> middle\n", "middle"],
      ["lazy line\n", "last"],
      ["> alone\n", "only"],
    ]);
    const box = (row: HTMLElement) => row.querySelector(".composer-quote-box")!;
    for (const row of rows()) {
      // The reply quote's bar and tint, reused, on a box that holds no text.
      expect(box(row)).toHaveClass("border-l-2", "border-l-primary/60", "bg-muted/40");
      expect(box(row)).toHaveClass("absolute", "inset-y-0", "-left-1.5", "-z-10");
      expect(box(row)).toHaveAttribute("aria-hidden", "true");
      expect(box(row).textContent).toBe("");
      expect(row).toHaveClass("relative", "isolate");
    }
    // Rounded only at the block's ends, so its rows meet without a seam.
    const corners = rows().map((row) => [
      box(row).classList.contains("rounded-t-md"),
      box(row).classList.contains("rounded-b-md"),
    ]);
    expect(corners).toEqual([
      [true, false],
      [false, false],
      [false, true],
      [true, true],
    ]);
    const plain = Array.from(layer()!.querySelectorAll<HTMLElement>(":scope div > div")).filter(
      (row) => !row.classList.contains("composer-quote"),
    );
    expect(plain.map((row) => row.textContent)).toEqual(["intro\n", "\n", "\n", "after"]);
    for (const row of plain) expect(row.querySelector(".composer-quote-box")).toBeNull();
  });

  it("re-places a block's rows when a quoted line joins or leaves it", () => {
    render(<Controlled initial={"> one\n\ntwo"} />);
    const places = () =>
      Array.from(layer()!.querySelectorAll<HTMLElement>(".composer-quote"), (row) => [
        row.textContent,
        row.dataset.quote,
      ]);
    expect(places()).toEqual([["> one\n", "only"]]);
    fireEvent.change(input(), { target: { value: "> one\n> two" } });
    expect(places()).toEqual([
      ["> one\n", "first"],
      ["> two", "last"],
    ]);
    fireEvent.change(input(), { target: { value: "> one\n\n> two" } });
    expect(places()).toEqual([
      ["> one\n", "only"],
      ["> two", "only"],
    ]);
  });

  it("carries a block quote through a quoted code block", () => {
    render(<Controlled initial={"> intro\n> ```\n> code\n> ```\n> outro"} />);
    const rows = Array.from(layer()!.querySelectorAll<HTMLElement>(".composer-quote"));
    expect(rows.map((row) => row.dataset.quote)).toEqual([
      "first",
      "middle",
      "middle",
      "middle",
      "last",
    ]);
    expect(
      layer()!.querySelector("[data-code-block]")!.querySelectorAll(".composer-quote"),
    ).toHaveLength(3);
  });

  it("keeps the dotted underline where italics can't be measured", () => {
    render(<Controlled initial="an _aside_ here" />);
    const italic = Array.from(layer()!.querySelectorAll<HTMLElement>(".md-em"));
    expect(italic.map((span) => span.textContent)).toEqual(["aside"]);
    expect(italic[0]).not.toHaveAttribute("data-slanted");
    const overlay = layer()!.querySelector("[data-italic-overlay]")!;
    expect(overlay.children).toHaveLength(0);
    expect(overlay).toHaveClass("composer-italic-overlay");
  });
});

describe("ComposerTextarea compact preview", () => {
  let setDraft: (value: string) => void = () => {};
  function Controlled({
    initial = "",
    ...props
  }: { initial?: string } & Partial<Parameters<typeof ComposerTextarea>[0]>) {
    const [value, setValue] = useState(initial);
    setDraft = setValue;
    return (
      <ComposerTextarea
        aria-label="Draft"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        {...props}
      />
    );
  }
  const input = () => screen.getByRole("textbox") as HTMLTextAreaElement;
  const view = () => screen.queryByTestId("composer-compact-view");
  const settle = (ms = 0) =>
    act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, ms);
      });
    });
  async function blurred(
    draft: string,
    props: Partial<Parameters<typeof ComposerTextarea>[0]> = {},
  ) {
    render(<Controlled initial={draft} {...props} />);
    act(() => input().focus());
    act(() => input().blur());
    await settle();
  }
  afterEach(() => {
    vi.restoreAllMocks();
    Reflect.deleteProperty(document, "caretPositionFromPoint");
    Reflect.deleteProperty(document, "caretRangeFromPoint");
  });

  it("collapses hidden markers once blurred, and restores the aligned layer on focus", async () => {
    const onCompactChange = vi.fn();
    await blurred("x **bold** and `code`\n```ts\nconst a = 1;\n```", { onCompactChange });
    expect(view()).toHaveTextContent("x bold and code ts const a = 1;");
    expect(view()?.textContent).toBe("x bold and code\nts\nconst a = 1;\n");
    expect(view()).toHaveAttribute("aria-hidden", "true");
    expect(view()?.parentElement).toHaveAttribute("data-compact");
    // The textarea stays the one input: focusable, just out of the flow and unpainted.
    expect(screen.getAllByRole("textbox")).toEqual([input()]);
    expect(input()).toHaveStyle({ opacity: "0", pointerEvents: "none" });
    // Nor any height of its own: no margin box, no line box strut.
    expect(view()?.parentElement).toHaveStyle({ lineHeight: "0" });
    // As tall as the textarea (jsdom lays nothing out, so 0 here).
    expect(input().style.marginBottom).not.toBe("");
    expect(screen.getByTestId("composer-highlight-overlay")).toHaveStyle({ opacity: "0" });
    expect(onCompactChange).toHaveBeenCalledTimes(1);

    act(() => input().focus());
    expect(view()).toBeNull();
    expect(input().style.opacity).toBe("");
    expect(input().style.marginBottom).toBe("");
    expect(screen.getByTestId("composer-highlight-overlay").style.opacity).toBe("");
    expect(onCompactChange).toHaveBeenCalledTimes(2);
    // The draft itself never changed.
    expect(input()).toHaveValue("x **bold** and `code`\n```ts\nconst a = 1;\n```");
  });

  it("starts compact when mounted unfocused, after the page had its chance to focus it", async () => {
    render(<Controlled initial="**restored** draft" />);
    expect(view()).toBeNull();
    await settle(60);
    expect(view()?.textContent).toBe("restored draft");
  });

  it("stays aligned when focus comes straight back", async () => {
    const onCompactChange = vi.fn();
    render(<Controlled initial="x **bold**" onCompactChange={onCompactChange} />);
    act(() => input().focus());
    act(() => {
      input().blur();
      input().focus();
    });
    await settle(60);
    expect(view()).toBeNull();
    expect(onCompactChange).not.toHaveBeenCalled();
  });

  it("waits for a press that blurred it to end, so its click lands first", async () => {
    render(<Controlled initial="x **bold**" />);
    act(() => input().focus());
    fireEvent.pointerDown(document.body);
    act(() => input().blur());
    await settle(10);
    expect(view()).toBeNull();
    fireEvent.pointerUp(document.body);
    await settle();
    expect(view()).not.toBeNull();
  });

  it("never shows the preview mid-press, however slow the page is after mount", () => {
    vi.useFakeTimers();
    try {
      render(<Controlled initial="x **bold**" />);
      // Within the unfocused-mount check's 50 ms: focus, then a press that blurs.
      act(() => input().focus());
      fireEvent.pointerDown(document.body);
      act(() => input().blur());
      act(() => vi.advanceTimersByTime(200));
      expect(view()).toBeNull();
      // Once the press ends, blur's own check shows it.
      fireEvent.pointerUp(document.body);
      act(() => vi.advanceTimersByTime(10));
      expect(view()).not.toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("drops the unfocused-mount check on an early press or focus", () => {
    vi.useFakeTimers();
    try {
      render(<Controlled initial="x **bold**" />);
      fireEvent.pointerDown(document.body);
      act(() => vi.advanceTimersByTime(200));
      expect(view()).toBeNull();
      cleanup();
      render(<Controlled initial="x **bold**" />);
      act(() => input().focus());
      act(() => vi.advanceTimersByTime(200));
      expect(view()).toBeNull();
      // Unmounted before it fires: nothing runs after.
      cleanup();
      render(<Controlled initial="x **bold**" />);
      cleanup();
      expect(() => act(() => vi.advanceTimersByTime(200))).not.toThrow();
    } finally {
      vi.useRealTimers();
    }
  });

  it("never compacts a draft with nothing hidden, or an empty one", async () => {
    await blurred("plain words, `unclosed and **open");
    expect(view()).toBeNull();
    cleanup();
    await blurred("");
    expect(view()).toBeNull();
  });

  /** The preview's text node holding exactly `text`. */
  function pieceText(text: string): Node {
    const walker = document.createTreeWalker(view()!, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode())
      if (node.textContent === text) return node;
    throw new Error(`no text node ${text}`);
  }

  it("puts the caret on the character clicked, and focuses the textarea", async () => {
    const draft = "x **bold** y";
    await blurred(draft);
    const point = vi.fn(() => ({ offsetNode: pieceText("bold"), offset: 2 }));
    Object.assign(document, { caretPositionFromPoint: point });
    fireEvent.mouseDown(view()!, { button: 0, clientX: 30, clientY: 5 });
    expect(point).toHaveBeenCalledWith(30, 5);
    expect(document.activeElement).toBe(input());
    expect([input().selectionStart, input().selectionEnd]).toEqual([6, 6]);
    expect(view()).toBeNull();
    // Just after the bold word: past its closing `**`, so typing continues plain.
    act(() => input().blur());
    await settle();
    point.mockReturnValue({ offsetNode: pieceText("bold"), offset: 4 });
    fireEvent.mouseDown(view()!, { button: 0 });
    expect(input().selectionStart).toBe(draft.indexOf(" y"));
  });

  it("falls back to caretRangeFromPoint, and to the draft's end off the text", async () => {
    await blurred("> quoted **line**");
    const range = document.createRange();
    range.setStart(pieceText("quoted "), 3);
    Object.assign(document, { caretRangeFromPoint: vi.fn(() => range) });
    fireEvent.mouseDown(view()!, { button: 0 });
    expect(input().selectionStart).toBe("> quo".length);
    act(() => input().blur());
    await settle();
    Object.assign(document, { caretRangeFromPoint: vi.fn(() => null) });
    fireEvent.mouseDown(view()!, { button: 0 });
    expect(input().selectionStart).toBe("> quoted **line**".length);
  });

  it("keeps the previous selection when focused by keyboard", async () => {
    render(<Controlled initial="x **bold** y" />);
    act(() => input().focus());
    input().setSelectionRange(4, 6);
    act(() => input().blur());
    await settle();
    expect(view()).not.toBeNull();
    act(() => input().focus());
    expect([input().selectionStart, input().selectionEnd]).toEqual([4, 6]);
  });

  it("follows a draft that changes while blurred (dictation, a restore)", async () => {
    await blurred("x **bold**");
    act(() => setDraft("x **bold** and _more_ words"));
    expect(view()?.textContent).toBe("x bold and more words");
    act(() => setDraft("now plain"));
    expect(view()).toBeNull();
  });

  it("replaces, adds and removes styled runs while blurred, with no stale text", async () => {
    await blurred("**bold** _italic_ **end**");
    expect(view()?.textContent).toBe("bold italic end");
    // Each step changes which styled runs a row holds, and where; `word` is
    // one of its styled words and `last` its final run.
    const steps: [string, string, string, string][] = [
      ["plain _italic_ **end**", "plain italic end", "italic", "end"],
      ["plain _italic_ **end** `code` ~~gone~~", "plain italic end code gone", "code", "gone"],
      ["**aa** **bb** **cc**", "aa bb cc", "bb", "cc"],
      ["**cc**", "cc", "cc", "cc"],
      ["_xx_ **why** `zz` [w](u) and **bold**", "xx why zz wu and bold", "why", "bold"],
    ];
    const click = async (node: Node, offset: number) => {
      Object.assign(document, { caretPositionFromPoint: () => ({ offsetNode: node, offset }) });
      fireEvent.mouseDown(view()!, { button: 0 });
      const at = input().selectionStart;
      act(() => input().blur());
      await settle();
      return at;
    };
    // Steps run in order: each edits the draft the previous one left.
    /* oxlint-disable no-await-in-loop */
    for (const [draft, shown, word, last] of steps) {
      act(() => setDraft(draft));
      expect(view()?.textContent).toBe(shown);
      // Clicks still land on the draft character under them: inside a word,
      // and at the very end (past any closing markers).
      expect(await click(pieceText(word), 1)).toBe(draft.indexOf(word) + 1);
      expect(await click(pieceText(last), last.length)).toBe(draft.length);
    }
    /* oxlint-enable no-await-in-loop */
  });

  it("does nothing on a click while disabled", async () => {
    render(<Controlled initial="x **bold**" disabled />);
    await settle(60);
    expect(view()).toHaveClass("opacity-60");
    fireEvent.mouseDown(view()!, { button: 0 });
    expect(document.activeElement).not.toBe(input());
  });
});
