import {
  cursorCharLeft,
  cursorCharRight,
  deleteCharBackward,
  deleteCharForward,
  redo,
  selectCharRight,
  undo,
} from "@codemirror/commands";
import { Transaction } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import userEvents from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { COMMAND_TOKEN_CLASS } from "@/components/composer/commandToken";
import { MENTION_TOKEN_CLASS } from "@/components/composer/MentionTokenOverlay";
import { insertComposerText, insertPastedText } from "@/lib/composerText";
import { quoteLine } from "@/lib/replyDraftDocument";
import { CodeMirrorComposerInput } from "./testing/codeMirrorComposer";
import { COMPOSER_EDITING_KEYMAP, leadingCommandSpan } from "./CodeMirrorComposerInput";
import {
  isComposerInput,
  isHistoryInput,
  type ComposerInputEvent,
  type ComposerKeyboardEvent,
  type ComposerPasteEvent,
  type ComposerTextTarget,
} from "./composerInput";

function mount(props: Partial<Parameters<typeof CodeMirrorComposerInput>[0]> = {}) {
  const box: { target: ComposerTextTarget | null } = { target: null };
  const setTarget = (target: ComposerTextTarget | null) => {
    box.target = target;
  };
  const view = render(
    <CodeMirrorComposerInput aria-label="Message the agent" value="" {...props} ref={setTarget} />,
  );
  const content = view.container.querySelector<HTMLElement>(".cm-content")!;
  return { ...view, content, setTarget, target: () => box.target! };
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("CodeMirrorComposerInput", () => {
  it.each([
    ["compact", "180px"],
    ["unbounded", "none"],
  ] as const)("sizes its scroller for the %s height", (heightMode, maxHeight) => {
    const { container } = mount({ heightMode });
    const scroller = getComputedStyle(container.querySelector(".cm-scroller")!);
    expect(scroller.maxHeight).toBe(maxHeight);
  });

  it("resizes its scroller when the height changes", () => {
    const { container, rerender, setTarget } = mount({ heightMode: "compact" });
    rerender(
      <CodeMirrorComposerInput
        aria-label="Message the agent"
        value=""
        heightMode="unbounded"
        ref={setTarget}
      />,
    );
    const scroller = getComputedStyle(container.querySelector(".cm-scroller")!);
    expect(scroller.maxHeight).toBe("none");
  });

  it("is a labelled multi-line composer input", () => {
    const { content } = mount();
    expect(isComposerInput(content)).toBe(true);
    expect(content.getAttribute("aria-label")).toBe("Message the agent");
    expect(content.getAttribute("aria-multiline")).toBe("true");
  });

  it("drops multi-line semantics when it is a combobox", () => {
    const { content, rerender, setTarget } = mount({ role: "combobox" });
    expect(content.getAttribute("role")).toBe("combobox");
    expect(content.hasAttribute("aria-multiline")).toBe(false);
    rerender(<CodeMirrorComposerInput aria-label="Message the agent" value="" ref={setTarget} />);
    expect(content.getAttribute("aria-multiline")).toBe("true");
  });

  it("hands keys to the composer first and skips CodeMirror's own binding when claimed", () => {
    const onKeyDown = vi.fn((event: ComposerKeyboardEvent) => event.preventDefault());
    const { content, target } = mount({ value: "draft", onKeyDown });
    act(() => target().setSelectionRange(5, 5));

    fireEvent.keyDown(content, { key: "Backspace" });

    expect(onKeyDown).toHaveBeenCalledTimes(1);
    expect(onKeyDown.mock.calls[0][0].currentTarget).toBe(target());
    expect(target().value).toBe("draft");
  });

  it("leaves keys alone during IME composition", () => {
    const onKeyDown = vi.fn();
    const { content } = mount({ onKeyDown });
    fireEvent.keyDown(content, { key: "Process", keyCode: 229 });
    fireEvent.keyDown(content, { key: "Enter", isComposing: true });
    expect(onKeyDown).not.toHaveBeenCalled();
  });

  it("passes a paste's clipboard data to the composer, which can claim it", () => {
    const onPaste = vi.fn((event: ComposerPasteEvent) => event.preventDefault());
    const { content, target } = mount({ value: "draft", onPaste });
    const clipboardData = { getData: () => "pasted", files: [], items: [], types: ["text/plain"] };

    fireEvent.paste(content, { clipboardData });

    expect(onPaste).toHaveBeenCalledTimes(1);
    expect(onPaste.mock.calls[0][0].currentTarget).toBe(target());
    expect(target().value).toBe("draft");
  });

  it("reports focus and blur", () => {
    const onFocus = vi.fn();
    const onBlur = vi.fn();
    const { target } = mount({ onFocus, onBlur });
    act(() => target().focus());
    act(() => target().blur());
    expect(onFocus).toHaveBeenCalledTimes(1);
    expect(onBlur).toHaveBeenCalledTimes(1);
  });

  it("can't be edited while disabled or read-only, and says so", () => {
    const { content, rerender } = mount({ disabled: true });
    expect(content.getAttribute("contenteditable")).toBe("false");
    expect(content.getAttribute("aria-disabled")).toBe("true");
    rerender(<CodeMirrorComposerInput value="" readOnly />);
    expect(content.getAttribute("aria-readonly")).toBe("true");
  });

  it("leaves spelling, autocorrect and capitalization on, as a textarea does", () => {
    const { content } = mount();
    expect(content.getAttribute("spellcheck")).toBe("true");
    expect(content.getAttribute("autocorrect")).toBe("on");
    expect(content.getAttribute("autocapitalize")).toBe("sentences");
  });

  it("locks while an ancestor fieldset is disabled, as a textarea does", async () => {
    const box: { target: ComposerTextTarget | null } = { target: null };
    const input = (fieldsetDisabled: boolean) => (
      <fieldset disabled={fieldsetDisabled}>
        <CodeMirrorComposerInput
          aria-label="Message the agent"
          defaultValue="draft"
          ref={(target) => {
            box.target = target;
          }}
        />
      </fieldset>
    );
    const { container, rerender } = render(input(true));
    const content = container.querySelector<HTMLElement>(".cm-content")!;
    expect(content.getAttribute("aria-disabled")).toBe("true");
    expect(insertComposerText(box.target!, "x", 0, 0)).toBe(false);
    expect(box.target!.value).toBe("draft");

    rerender(input(false));
    await waitFor(() => expect(content.getAttribute("aria-disabled")).toBe("false"));
    expect(insertComposerText(box.target!, "x", 0, 0)).toBe(true);
    expect(box.target!.value).toBe("xdraft");

    rerender(input(true));
    await waitFor(() => expect(content.getAttribute("aria-disabled")).toBe("true"));
    expect(insertComposerText(box.target!, "y", 0, 0)).toBe(false);
    expect(box.target!.value).toBe("xdraft");
  });

  it("stays editable inside a disabled fieldset's first legend, as a textarea does", () => {
    const { container } = render(
      <fieldset disabled>
        <legend>
          <CodeMirrorComposerInput aria-label="Message the agent" defaultValue="draft" />
        </legend>
      </fieldset>,
    );
    expect(container.querySelector(".cm-content")!.getAttribute("aria-disabled")).toBe("false");
  });

  it("takes a caller's text size in place of the composer's own", () => {
    const host = (className?: string) => {
      const { container, unmount } = mount({ className });
      const classes = [...container.querySelector("[data-composer-engine]")!.classList];
      unmount();
      return classes;
    };
    expect(host()).toContain("text-ui");
    const sized = host("text-sm");
    expect(sized).toContain("text-sm");
    expect(sized).not.toContain("text-ui");
    expect(sized).toContain("text-foreground");
  });
});

describe("CodeMirrorComposerInput change events", () => {
  it.each([undefined, { separateUndo: true }])(
    "makes a programmatic insert (%o) its own undo step apart from typing",
    (options) => {
      const { content, target } = mount({ value: "" });
      const editor = EditorView.findFromDOM(content)!;
      act(
        () =>
          void editor.dispatch({ changes: { from: 0, insert: "typed" }, userEvent: "input.type" }),
      );
      act(() => insertComposerText(target(), " inserted", 5, 5, options));
      expect(target().value).toBe("typed inserted");
      act(() => void undo(editor));
      expect(target().value).toBe("typed");
    },
  );

  it("says when an undo or redo made the change", () => {
    const onChange = vi.fn((event: ComposerInputEvent) => isHistoryInput(event));
    const { content, target } = mount({ onChange });
    act(() => {
      insertComposerText(target(), "draft", 0, 0);
    });
    const editor = EditorView.findFromDOM(content)!;
    act(() => void undo(editor));
    act(() => void redo(editor));
    expect(onChange.mock.results.map((result) => result.value)).toEqual([false, true, true]);
  });

  it("reports an undo as 'undo' and a redo as 'redo', and an ordinary edit with no direction", () => {
    const onChange = vi.fn<(event: ComposerInputEvent) => void>();
    const { content, target } = mount({ onChange });
    act(() => {
      insertComposerText(target(), "draft", 0, 0);
    });
    const editor = EditorView.findFromDOM(content)!;
    act(() => void undo(editor));
    act(() => void redo(editor));
    expect(onChange.mock.calls.map(([event]) => event.historyDirection)).toEqual([
      undefined,
      "undo",
      "redo",
    ]);
  });

  it("takes a controlled value without reporting it or making it undoable", () => {
    const onChange = vi.fn();
    const { content, target, rerender, setTarget } = mount({ value: "", onChange });
    act(() => insertComposerText(target(), "hello", 0, 0));
    act(() => insertComposerText(target(), "", 3, 5));
    expect(target().value).toBe("hel");
    expect(onChange).toHaveBeenCalledTimes(2);
    rerender(
      <CodeMirrorComposerInput
        aria-label="Message the agent"
        value="hel"
        onChange={onChange}
        ref={setTarget}
      />,
    );
    rerender(
      <CodeMirrorComposerInput
        aria-label="Message the agent"
        value=""
        onChange={onChange}
        ref={setTarget}
      />,
    );
    expect(target().value).toBe("");
    expect(onChange).toHaveBeenCalledTimes(2);

    const editor = EditorView.findFromDOM(content)!;
    act(() => void undo(editor));
    expect(target().value).toBe("");
    act(() => void redo(editor));
    expect(target().value).toBe("");
  });

  it("reports a selection change, but not a controlled value change, as a selection", () => {
    const onSelect = vi.fn();
    const { target, rerender, setTarget } = mount({ value: "draft", onSelect });

    rerender(
      <CodeMirrorComposerInput
        aria-label="Message the agent"
        value="other draft"
        onSelect={onSelect}
        ref={setTarget}
      />,
    );
    expect(target().value).toBe("other draft");
    expect(onSelect).not.toHaveBeenCalled();

    act(() => target().setSelectionRange(2, 2));
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("keeps an edit the parent ignores until the value changes", () => {
    const { target, rerender, setTarget } = mount({ value: "draft" });
    act(() => insertComposerText(target(), "x", 5, 5));

    rerender(
      <CodeMirrorComposerInput aria-label="Message the agent" value="draft" ref={setTarget} />,
    );
    expect(target().value).toBe("draftx");

    rerender(
      <CodeMirrorComposerInput aria-label="Message the agent" value="next" ref={setTarget} />,
    );
    expect(target().value).toBe("next");
  });

  it("puts the caret at the end of a longer value after a caret move past the old end", () => {
    // A caret set before the new value commits clamps to the old end, as a mention pick's does.
    const { target, rerender, setTarget } = mount({ value: "Check " });
    act(() => target().setSelectionRange(99, 99));
    expect(target().selectionStart).toBe("Check ".length);

    rerender(
      <CodeMirrorComposerInput
        aria-label="Message the agent"
        value="Check @README.md"
        ref={setTarget}
      />,
    );
    expect([target().selectionStart, target().selectionEnd]).toEqual([16, 16]);
  });

  it("normalizes a controlled CRLF value to the document's line breaks", () => {
    const { target, rerender, setTarget } = mount({ value: "" });
    rerender(
      <CodeMirrorComposerInput aria-label="Message the agent" value={"a\r\nb"} ref={setTarget} />,
    );
    expect(target().value).toBe("a\nb");
    expect([target().selectionStart, target().selectionEnd]).toEqual([3, 3]);
  });

  it("inserts CRLF text as the document's line breaks", () => {
    const onChange = vi.fn();
    const { target } = mount({ value: undefined, onChange });

    act(() => insertComposerText(target(), "x\r\ny", 0, 0));

    expect(target().value).toBe("x\ny");
    expect([target().selectionStart, target().selectionEnd]).toEqual([3, 3]);
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("gives each programmatic insert its own undo step", () => {
    const { content, target } = mount();
    act(() => insertComposerText(target(), "a", 0, 0));
    act(() => insertComposerText(target(), "b", 1, 1));

    act(() => void undo(EditorView.findFromDOM(content)!));
    expect(target().value).toBe("a");
  });

  it("lets a change handler move the caret without re-entering an update", () => {
    const onChange = vi.fn(() => target().setSelectionRange(0, 0));
    const { target } = mount({ onChange });

    act(() => insertComposerText(target(), "x", 0, 0));

    expect(target().value).toBe("x");
    expect([target().selectionStart, target().selectionEnd]).toEqual([0, 0]);
  });
});

describe("CodeMirrorComposerInput mention edges", () => {
  const painted = ["@readme.md"];
  function insertAt(from: number, insert: string, userEvent: string, tokens: string[]) {
    const { content } = mount({ value: "Read @readme.md", mentionTokens: tokens });
    const editor = EditorView.findFromDOM(content)!;
    act(() => void editor.dispatch({ changes: { from, insert }, userEvent }));
    return editor.state.doc.toString();
  }

  it("adds the space to a typed insert beside a painted token", () => {
    expect(insertAt(15, "x", "input.type", painted)).toBe("Read @readme.md x");
  });

  it("adds no space beside a token the editor does not paint", () => {
    expect(insertAt(15, "x", "input.type", [])).toBe("Read @readme.mdx");
  });

  it("keeps a programmatic insert beside a painted token as its own undo step", () => {
    const { content, target } = mount({ value: "", mentionTokens: painted });
    const editor = EditorView.findFromDOM(content)!;
    act(
      () =>
        void editor.dispatch({
          changes: { from: 0, insert: "Read @readme.md" },
          userEvent: "input.type",
        }),
    );
    act(() => insertComposerText(target(), "x", 15, 15));
    expect(editor.state.doc.toString()).toBe("Read @readme.md x");
    act(() => void undo(editor));
    expect(editor.state.doc.toString()).toBe("Read @readme.md");
  });

  it("keeps an adjusted insert that is marked out of history out of undo", () => {
    const { content } = mount({ value: "", mentionTokens: painted });
    const editor = EditorView.findFromDOM(content)!;
    act(
      () =>
        void editor.dispatch({
          changes: { from: 0, insert: "Read @readme.md" },
          userEvent: "input.type",
        }),
    );
    act(
      () =>
        void editor.dispatch({
          changes: { from: 15, insert: "x" },
          userEvent: "input",
          annotations: Transaction.addToHistory.of(false),
        }),
    );
    expect(editor.state.doc.toString()).toBe("Read @readme.md x");
    act(() => void undo(editor));
    expect(editor.state.doc.toString()).toBe(" x");
  });

  it("leaves an IME composition insert beside a painted token unchanged", () => {
    expect(insertAt(15, "x", "input.type.compose", painted)).toBe("Read @readme.mdx");
  });
});

describe("CodeMirrorComposerInput ref", () => {
  it("transfers the live surface when the forwarded ref changes", () => {
    const first = vi.fn<(target: ComposerTextTarget | null) => void>();
    const second = vi.fn<(target: ComposerTextTarget | null) => void>();
    const { rerender } = render(
      <CodeMirrorComposerInput aria-label="Message the agent" value="" ref={first} />,
    );
    const surface = first.mock.lastCall![0]!;

    rerender(<CodeMirrorComposerInput aria-label="Message the agent" value="" ref={second} />);

    expect(first).toHaveBeenLastCalledWith(null);
    expect(second).toHaveBeenLastCalledWith(surface);
  });
});

describe("COMPOSER_EDITING_KEYMAP", () => {
  it("leaves Enter to the composer and claims no Control+Option chord", () => {
    const keys = COMPOSER_EDITING_KEYMAP.flatMap((binding) =>
      [binding.key, binding.mac, binding.win, binding.linux].filter(
        (key): key is string => key !== undefined,
      ),
    );
    expect(keys).not.toContain("Enter");
    expect(keys.filter((key) => /Ctrl-/.test(key) && /Alt-/.test(key))).toEqual([]);
  });
});

describe("CodeMirrorComposerInput readline keys off macOS", () => {
  it("leaves ⌃W unclaimed, so the caret and draft stay put", () => {
    const { content, target } = mount({ value: "git push origin" });
    act(() => target().setSelectionRange(15, 15));

    const notClaimed = fireEvent.keyDown(content, { key: "w", code: "KeyW", ctrlKey: true });

    expect(notClaimed).toBe(true);
    expect(target().value).toBe("git push origin");
    expect(target().selectionStart).toBe(15);
  });

  it("keeps ⌃A as Select All, its platform meaning, instead of the line start", () => {
    const { content, target } = mount({ value: "git push origin" });
    act(() => target().setSelectionRange(15, 15));

    fireEvent.keyDown(content, { key: "a", code: "KeyA", ctrlKey: true });

    expect(target().selectionStart).toBe(0);
    expect(target().selectionEnd).toBe("git push origin".length);
  });
});

describe("CodeMirrorComposerInput mention tokens", () => {
  const tokenMarks = (container: HTMLElement) =>
    Array.from(container.querySelectorAll('[data-testid="composer-mention-token"]')).map(
      (token) => [token.textContent, token.className],
    );

  it("paints each tagged token with the mention class, and no other text", () => {
    const { container } = mount({
      value: "see @a.ts and @a.ts:2-4 then a@b.ts",
      mentionTokens: ["@a.ts", "@a.ts:2-4"],
    });
    expect(tokenMarks(container)).toEqual([
      ["@a.ts", MENTION_TOKEN_CLASS],
      ["@a.ts:2-4", MENTION_TOKEN_CLASS],
    ]);
  });

  it("refreshes the marks when the tokens change without a text edit", () => {
    const view = mount({ value: "see @a.ts", mentionTokens: ["@a.ts"] });
    expect(tokenMarks(view.container)).toHaveLength(1);

    view.rerender(
      <CodeMirrorComposerInput
        aria-label="Message the agent"
        value="see @a.ts"
        mentionTokens={[]}
      />,
    );
    expect(tokenMarks(view.container)).toEqual([]);
  });

  it("marks a token typed after mount, and drops it once an edit breaks the match", () => {
    const { container, target } = mount({ mentionTokens: ["@a.ts"] });
    expect(tokenMarks(container)).toEqual([]);

    act(() => {
      insertComposerText(target(), "@a.ts", 0, 0);
    });
    expect(tokenMarks(container)).toHaveLength(1);

    act(() => {
      insertComposerText(target(), "", 0, 1);
    });
    expect(tokenMarks(container)).toEqual([]);
  });
});

/** CodeMirror measures text for caret motion; jsdom has no layout, so ranges measure empty. */
function mockEmptyRangeGeometry() {
  const originalRangeRects = {
    getClientRects: Range.prototype.getClientRects,
    getBoundingClientRect: Range.prototype.getBoundingClientRect,
  };
  beforeAll(() => {
    Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
    Range.prototype.getBoundingClientRect = () => new DOMRect();
  });
  afterAll(() => Object.assign(Range.prototype, originalRangeRects));
}

describe("CodeMirrorComposerInput mention tokens move as one character", () => {
  mockEmptyRangeGeometry();

  const draft = "Read @readme.md now";
  const tokenStart = "Read ".length;
  const tokenEnd = "Read @readme.md".length;
  function mountDraft() {
    const mounted = mount({ value: draft, mentionTokens: ["@readme.md"] });
    return { ...mounted, editor: EditorView.findFromDOM(mounted.content)! };
  }
  const caretAt = (editor: EditorView, offset: number) =>
    act(() => editor.dispatch({ selection: { anchor: offset } }));

  it("steps the caret over a token in one move, in both directions", () => {
    const { editor } = mountDraft();
    caretAt(editor, tokenEnd);
    act(() => void cursorCharLeft(editor));
    expect(editor.state.selection.main.head).toBe(tokenStart);
    act(() => void cursorCharRight(editor));
    expect(editor.state.selection.main.head).toBe(tokenEnd);
  });

  it("extends a selection over the whole token", () => {
    const { editor } = mountDraft();
    caretAt(editor, tokenStart);
    act(() => void selectCharRight(editor));
    expect(editor.state.selection.main).toMatchObject({ from: tokenStart, to: tokenEnd });
  });

  it("Backspace after a token removes the whole token", () => {
    const { editor, target } = mountDraft();
    caretAt(editor, tokenEnd);
    act(() => void deleteCharBackward(editor));
    expect(target().value).toBe("Read  now");
  });

  it("leaves plain text around a token to move one character at a time", () => {
    const { editor } = mountDraft();
    caretAt(editor, tokenEnd + 2);
    act(() => void cursorCharLeft(editor));
    expect(editor.state.selection.main.head).toBe(tokenEnd + 1);
  });
});

describe("CodeMirrorComposerInput command token", () => {
  const commandMarks = (container: HTMLElement) =>
    Array.from(container.querySelectorAll('[data-testid="composer-command-token"]')).map(
      (token) => [token.textContent, token.className],
    );

  it("tints the leading command like the textarea's mirror, leaving its arguments plain", () => {
    const { container } = mount({ value: "  /context review this", commandToken: "/context" });
    expect(commandMarks(container)).toEqual([["/context", COMMAND_TOKEN_CLASS]]);
  });

  it("follows the token as it changes, and marks nothing for prose", () => {
    const view = mount({ value: "/context", commandToken: "/context" });
    view.rerender(
      <CodeMirrorComposerInput
        aria-label="Message the agent"
        value="/context"
        commandToken={null}
      />,
    );
    expect(commandMarks(view.container)).toEqual([]);
    view.rerender(
      <CodeMirrorComposerInput
        aria-label="Message the agent"
        value="/review"
        commandToken="/review"
      />,
    );
    expect(commandMarks(view.container)).toEqual([["/review", COMMAND_TOKEN_CLASS]]);
  });

  it("tints a placed `!` behind a control character, with its command right after it", () => {
    const { container } = mount({ value: "\x01!pwd", commandToken: { token: "!", start: 1 } });
    expect(commandMarks(container)).toEqual([["!", COMMAND_TOKEN_CLASS]]);
  });

  it("places a token by the value's offset when the editor folds its CRLF line breaks", () => {
    const view = mount({ value: "\r\n!ls", commandToken: { token: "!", start: 2 } });
    expect(commandMarks(view.container)).toEqual([["!", COMMAND_TOKEN_CLASS]]);

    // A new value and token reconfigure the mark through the same offset mapping.
    view.rerender(
      <CodeMirrorComposerInput
        aria-label="Message the agent"
        value={"\r\n\r\n!ls"}
        commandToken={{ token: "!", start: 4 }}
      />,
    );
    expect(commandMarks(view.container)).toEqual([["!", COMMAND_TOKEN_CLASS]]);
    expect(view.container.querySelector(".cm-content")?.textContent).toBe("!ls");
  });

  it("never marks text the token no longer leads, even before the token prop catches up", () => {
    const { container, target } = mount({ value: "/context", commandToken: "/context" });
    act(() => {
      insertComposerText(target(), "s", "/context".length, "/context".length);
    });
    expect(commandMarks(container)).toEqual([]);
  });
});

describe("leadingCommandSpan", () => {
  it("finds the token after leading whitespace, and only as a whole leading word", () => {
    expect(leadingCommandSpan("/context", "/context")).toEqual({ start: 0, end: 8 });
    expect(leadingCommandSpan("\n  /context now", "/context")).toEqual({ start: 3, end: 11 });
    // A known command's full name may hold a space.
    expect(leadingCommandSpan("/review pr now", "/review pr")).toEqual({ start: 0, end: 10 });
    expect(leadingCommandSpan("/contexts", "/context")).toBeNull();
    expect(leadingCommandSpan("say /context", "/context")).toBeNull();
    expect(leadingCommandSpan("/context", null)).toBeNull();
  });

  it("places a positioned token only where the text still holds it", () => {
    expect(leadingCommandSpan("!ls", { token: "!", start: 0 })).toEqual({ start: 0, end: 1 });
    expect(leadingCommandSpan("\x01!ls", { token: "!", start: 1 })).toEqual({ start: 1, end: 2 });
    expect(leadingCommandSpan("ls", { token: "!", start: 0 })).toBeNull();
    // The bare-string rule would refuse both: no word break after `!`, and \x01 isn't whitespace.
    expect(leadingCommandSpan("!ls", "!")).toBeNull();
    expect(leadingCommandSpan("\x01! ls", "!")).toBeNull();
  });
});

describe("CodeMirrorComposerInput reply quotes", () => {
  mockEmptyRangeGeometry();

  const a = quoteLine("a", "Alpha");
  const b = quoteLine("b", "Beta\nmore");
  const lines = (...parts: string[]) => parts.join("\n");
  function mountQuotes(value: string, props: Partial<Parameters<typeof mount>[0]> = {}) {
    const mounted = mount({ value, replyQuotes: true, ...props });
    const editor = EditorView.findFromDOM(mounted.content)!;
    const caretAt = (anchor: number, head = anchor) =>
      act(() => editor.dispatch({ selection: { anchor, head } }));
    const run = (command: (view: EditorView) => boolean) => act(() => void command(editor));
    const edit = (from: number, to: number, insert: string, userEvent = "input.type") =>
      act(() =>
        editor.dispatch({
          changes: { from, to, insert },
          selection: { anchor: from + insert.length },
          userEvent,
        }),
      );
    const removeButtons = () =>
      Array.from(
        mounted.container.querySelectorAll<HTMLButtonElement>("[aria-label='Remove quote']"),
      );
    return { ...mounted, editor, caretAt, run, edit, removeButtons };
  }

  it("paints each quote as a card with a truncated preview and an X that follows read-only", () => {
    const long = "x".repeat(130);
    const value = lines("Intro", quoteLine("long", long), "");
    const { container, rerender, setTarget, removeButtons } = mountQuotes(value);
    const card = container.querySelector("[data-testid='composer-reply-quote']")!;
    expect(card.querySelector("blockquote")).toHaveAttribute("title", long);
    expect(card.querySelector("[aria-hidden='true']")!.textContent).toBe(`${"x".repeat(120)}…`);
    expect(card.querySelector(".sr-only")!.textContent).toBe(long);
    expect(removeButtons()[0]).toBeEnabled();
    rerender(
      <CodeMirrorComposerInput
        aria-label="Message the agent"
        value={value}
        replyQuotes
        readOnly
        ref={setTarget}
      />,
    );
    expect(removeButtons()[0]).toBeDisabled();
  });

  it("keeps the caret off cards, stepping over one with ← and →", () => {
    const { content, editor, caretAt } = mountQuotes(lines(a, "Intro", b, "Reply"));
    const head = () => editor.state.selection.main.head;
    expect(head()).toBe(a.length + 1);
    fireEvent.keyDown(content, { key: "ArrowLeft" });
    expect(head()).toBe(a.length + 1);
    caretAt(lines(a, "Intro").length);
    fireEvent.keyDown(content, { key: "ArrowRight" });
    expect(head()).toBe(lines(a, "Intro", b, "").length);
    fireEvent.keyDown(content, { key: "ArrowLeft" });
    expect(head()).toBe(lines(a, "Intro").length);
  });

  it("puts the caret on the line after a card when the card is clicked", () => {
    const { container, editor, target, caretAt } = mountQuotes(lines("Intro", a, "Reply"));
    caretAt(0);
    const quote = container.querySelector("[data-testid='composer-reply-quote'] blockquote span")!;
    expect(fireEvent.mouseDown(quote)).toBe(false);
    expect(editor.state.selection.main.head).toBe(lines("Intro", a, "").length);
    expect(editor.hasFocus).toBe(true);
    act(() => insertComposerText(target(), "#"));
    expect(target().value).toBe(lines("Intro", a, "#Reply"));
  });

  it.each([
    ["at the start of the document", 0, lines("#", a, "Mid", b, "")],
    ["at a card's end", a.length, lines(a, "#", "Mid", b, "")],
    ["at a card's start", lines(a, "Mid", "").length, lines(a, "Mid", "#", b, "")],
  ])("moves text typed, pasted or dropped %s to a line of its own", (_where, at, expected) => {
    const { target, edit } = mountQuotes(lines(a, "Mid", b, ""));
    edit(at, at, "#");
    expect(target().value).toBe(expected);
    expect(target().selectionStart).toBe(expected.indexOf("#") + 1);

    for (const userEvent of ["input.paste", "input.drop"]) {
      const inserted = mountQuotes(lines(a, "Mid", b, ""));
      inserted.edit(at, at, `${quoteLine("z", "Zed")}#`, userEvent);
      expect(inserted.target().value).toBe(expected);
    }
  });

  it("deletes a quote typed over whole, and keeps one beside a typed-over break", () => {
    const value = lines("Intro", a, "Reply");
    const { target, edit, run } = mountQuotes(value);
    edit("Intro\n".length, "Intro\n".length + a.length, "x");
    expect(target().value).toBe(lines("Intro", "x", "Reply"));
    run(undo);
    expect(target().value).toBe(value);
    edit("Intro".length, "Intro\n".length, "x");
    expect(target().value).toBe(lines("Introx", a, "Reply"));
  });

  it("keeps a card when only the break after it is selected and deleted", () => {
    const value = lines("Intro", a, "Reply");
    const { target, caretAt, run } = mountQuotes(value);
    caretAt(lines("Intro", a).length, lines("Intro", a, "").length);
    run(deleteCharBackward);
    expect(target().value).toBe(value);
  });

  it("drops only the part of an edit inside a token", () => {
    const { target, editor } = mountQuotes(lines("Intro", a, "Reply"));
    act(() =>
      editor.dispatch({
        changes: [
          { from: 0, insert: "Hi " },
          { from: "Intro\n".length + 3, insert: "zz" },
        ],
        userEvent: "input.type",
      }),
    );
    expect(target().value).toBe(lines("Hi Intro", a, "Reply"));
    // An insert whose range ends inside the token still lands, and the token stays whole.
    act(() => insertComposerText(target(), "abc", 2, "Hi Intro\n".length + 3));
    expect(target().value).toBe(lines("Hiabc", a, "Reply"));
  });

  it.each([
    [
      "Backspace under the last card",
      lines("Opener", a, "", b, "").length,
      deleteCharBackward,
      lines("Opener", a, ""),
    ],
    ["Delete before the first card", "Opener".length, deleteCharForward, lines("Opener", b, "")],
  ])("removes a card with %s, which undo brings back exactly", (_key, caret, command, removed) => {
    const value = lines("Opener", a, "", b, "");
    const { target, caretAt, run } = mountQuotes(value);
    caretAt(caret);
    run(command);
    expect(target().value).toBe(removed);
    run(undo);
    expect(target().value).toBe(value);
    run(redo);
    expect(target().value).toBe(removed);
    run(undo);
    expect(target().value).toBe(value);
  });

  it.each([
    ["first", 0, lines("Opener", b, "")],
    ["last", 1, lines("Opener", a, "")],
  ])(
    "removes the %s card with its X after an undo, and undo brings it back",
    async (_which, index, removed) => {
      const value = lines("Opener", a, "", b, "");
      const { target, editor, caretAt, run, removeButtons } = mountQuotes(value);
      caretAt(value.length);
      run(deleteCharBackward);
      run(undo);
      // From the keyboard, so the X holds focus when it goes, and undo is a key to the editor.
      const user = userEvents.setup();
      act(() => removeButtons()[index]!.focus());
      await user.keyboard("{Enter}");
      expect(target().value).toBe(removed);
      expect(editor.hasFocus).toBe(true);
      await user.keyboard("{Control>}z{/Control}");
      expect(target().value).toBe(value);
    },
  );

  it("removes adjacent cards one per keystroke", () => {
    const { target, caretAt, run } = mountQuotes(lines(a, b, "Reply"));
    caretAt(lines(a, b, "").length);
    run(deleteCharBackward);
    expect(target().value).toBe(lines(a, "Reply"));
    run(deleteCharBackward);
    expect(target().value).toBe("Reply");

    const separated = mountQuotes(lines(a, "", b, "Reply"));
    separated.caretAt(lines(a, "").length);
    separated.run(deleteCharBackward);
    expect(separated.target().value).toBe(lines(b, "Reply"));
  });

  it("copies, drags and cuts quotes as markdown", () => {
    const value = lines("Intro", b, "Reply");
    const markdown = lines("Intro", "> Beta", "> more", "Reply");
    const { content, caretAt, target, run } = mountQuotes(value);
    act(() => target().focus());
    caretAt(0, value.length);
    const setData = vi.fn();
    fireEvent.copy(content, { clipboardData: { setData, clearData: vi.fn() } });
    expect(setData).toHaveBeenLastCalledWith("text/plain", markdown);
    fireEvent.dragStart(content, { dataTransfer: { setData } });
    expect(setData).toHaveBeenLastCalledWith("Text", markdown);
    fireEvent.cut(content, { clipboardData: { setData, clearData: vi.fn() } });
    expect(setData).toHaveBeenLastCalledWith("text/plain", markdown);
    expect(target().value).toBe("");
    run(undo);
    expect(target().value).toBe(value);
  });

  it("strips tokens from a paste the composer inserts itself", () => {
    const onPaste = (event: ComposerPasteEvent) => void insertPastedText(event);
    const { content, target, caretAt } = mountQuotes(lines("Intro", a, ""), { onPaste });
    caretAt(lines("Intro", a, "").length);
    const clipboardData = { getData: () => `pasted ${b} text`, files: [], items: [], types: [] };
    fireEvent.paste(content, { clipboardData });
    expect(target().value).toBe(lines("Intro", a, "pasted  text"));
  });

  it("takes a controlled value that removes a middle card as it is", () => {
    const c = quoteLine("c", "Gamma");
    const { target, rerender, setTarget } = mountQuotes(lines("A", a, "B", b, "C", c, ""));
    const removed = lines("A", a, "B", "C", c, "");
    rerender(
      <CodeMirrorComposerInput
        aria-label="Message the agent"
        value={removed}
        replyQuotes
        ref={setTarget}
      />,
    );
    expect(target().value).toBe(removed);
  });

  it("keeps token text literally in a composer without reply quotes", () => {
    const value = lines("Intro", a, "");
    const { container, target } = mount({ value });
    expect(container.querySelector("[data-testid='composer-reply-quote']")).toBeNull();
    act(() => insertComposerText(target(), b, 0, 0));
    expect(target().value).toBe(b + value);
  });
});
