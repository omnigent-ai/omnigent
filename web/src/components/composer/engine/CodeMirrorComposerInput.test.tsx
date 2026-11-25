import {
  cursorCharLeft,
  cursorCharRight,
  deleteCharBackward,
  redo,
  selectCharRight,
  undo,
} from "@codemirror/commands";
import { Transaction } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { COMMAND_TOKEN_CLASS } from "@/components/composer/commandToken";
import { MENTION_TOKEN_CLASS } from "@/components/composer/MentionTokenOverlay";
import { insertComposerText } from "@/lib/composerText";
import { replyQuoteLine, settleReplyDocument } from "@/lib/replyDraftDocument";
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

describe("CodeMirrorComposerInput mention tokens move as one character", () => {
  // CodeMirror measures text for caret motion; jsdom has no layout.
  const originalRangeRects = {
    getClientRects: Range.prototype.getClientRects,
    getBoundingClientRect: Range.prototype.getBoundingClientRect,
  };
  beforeAll(() => {
    Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
    Range.prototype.getBoundingClientRect = () => new DOMRect();
  });
  afterAll(() => Object.assign(Range.prototype, originalRangeRects));

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

describe("CodeMirrorComposerInput reply cards", () => {
  // CodeMirror measures text for caret motion; jsdom has no layout.
  const originalRangeRects = {
    getClientRects: Range.prototype.getClientRects,
    getBoundingClientRect: Range.prototype.getBoundingClientRect,
  };
  beforeAll(() => {
    Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
    Range.prototype.getBoundingClientRect = () => new DOMRect();
  });
  afterAll(() => Object.assign(Range.prototype, originalRangeRects));

  const card = (text: string, id = "card") => replyQuoteLine({ id, text });
  const mountDocument = (value: string, props: Partial<Parameters<typeof mount>[0]> = {}) => {
    const mounted = mount({ value, ...props });
    return { ...mounted, editor: EditorView.findFromDOM(mounted.content)! };
  };
  const caret = (editor: EditorView, offset: number) =>
    act(() => void editor.dispatch({ selection: { anchor: offset } }));
  const typeAt = (editor: EditorView, from: number, insert: string, to = from) =>
    act(
      () =>
        void editor.dispatch({
          changes: { from, to, insert },
          userEvent: "input.type",
        }),
    );
  const pasteAt = (editor: EditorView, from: number, insert: string, to = from) =>
    act(
      () =>
        void editor.dispatch({
          changes: { from, to, insert },
          userEvent: "input.paste",
        }),
    );
  const key = (content: HTMLElement, init: KeyboardEventInit) =>
    act(() => void fireEvent.keyDown(content, init));

  it("paints each card with a truncated preview and the full text for screen readers", () => {
    const long = "x".repeat(130);
    const { container, content } = mountDocument(`Opener\n${card(long)}\nWhy`);
    const row = container.querySelector<HTMLElement>('[data-testid="composer-reply-quote"]')!;
    const quote = row.querySelector("blockquote")!;
    expect(quote.getAttribute("title")).toBe(long);
    expect(quote.querySelector('[aria-hidden="true"]')!.textContent).toBe(`${"x".repeat(120)}…`);
    expect(quote.querySelector(".sr-only")!.textContent).toBe(long);
    expect(row.querySelector('button[aria-label="Remove quote"]')).not.toBeNull();
    // The editor lets a long card shrink, so its X stays on screen.
    expect(getComputedStyle(content).minWidth).toBe("0px");
  });

  it("disables a card's X while the draft is read-only", () => {
    const { container } = mountDocument(`${card("Quoted")}\nWhy`, { readOnly: true });
    const remove = container.querySelector<HTMLButtonElement>('button[aria-label="Remove quote"]')!;
    expect(remove.disabled).toBe(true);
  });

  it("steps the arrow keys over a card, to the text on either side", () => {
    const value = `Opener\n${card("Quoted")}\nWhy`;
    const { content, editor } = mountDocument(value);
    caret(editor, value.indexOf("Why"));
    key(content, { key: "ArrowLeft" });
    expect(editor.state.selection.main.head).toBe("Opener".length);
    key(content, { key: "ArrowRight" });
    expect(editor.state.selection.main.head).toBe(value.indexOf("Why"));
  });

  it("puts typed text at a card's start or end on its own line", () => {
    const quoted = card("Quoted");
    const value = `Opener\n${quoted}\nWhy`;
    const start = value.indexOf(quoted);
    const end = start + quoted.length;
    const first = mountDocument(value);
    typeAt(first.editor, start, "x");
    expect(first.target().value).toBe(`Opener\nx\n${quoted}\nWhy`);
    const last = mountDocument(value);
    typeAt(last.editor, end, "x");
    expect(last.target().value).toBe(`Opener\n${quoted}\nx\nWhy`);
  });

  it("puts typed text at offset 0 on its own line above the first card", () => {
    const quoted = card("Quoted");
    const { editor } = mountDocument(`${quoted}\nWhy`);
    typeAt(editor, 0, "x");
    expect(editor.state.doc.toString()).toBe(`x\n${quoted}\nWhy`);
  });

  it("puts pasted text at a card's start or end on its own line", () => {
    const quoted = card("Quoted");
    const value = `Opener\n${quoted}\nWhy`;
    const start = value.indexOf(quoted);
    const first = mountDocument(value);
    pasteAt(first.editor, start, "pasted");
    expect(first.editor.state.doc.toString()).toBe(`Opener\npasted\n${quoted}\nWhy`);
    const last = mountDocument(value);
    pasteAt(last.editor, start + quoted.length, "pasted");
    expect(last.editor.state.doc.toString()).toBe(`Opener\n${quoted}\npasted\nWhy`);
  });

  it("drops a card pasted as text, so no duplicate card appears", () => {
    const { editor } = mountDocument(`Opener\nWhy`);
    pasteAt(editor, "Opener\n".length, card("Pasted", "copy"));
    expect(editor.state.doc.toString()).toBe("Opener\nWhy");
  });

  it.each([
    ["at a card's start", `Opener\n${card("Quoted")}\nWhy`, 7],
    ["at a card's end", `Opener\n${card("Quoted")}\nWhy`, 7 + card("Quoted").length],
    ["above the first card", `${card("Quoted")}\nWhy`, 0],
  ])("settles typed text %s as settleReplyDocument does", (_name, value, offset) => {
    const { editor } = mountDocument(value);
    typeAt(editor, offset, "x");
    expect(editor.state.doc.toString()).toBe(
      settleReplyDocument(value.slice(0, offset) + "x" + value.slice(offset)),
    );
  });

  it("removes a card when typing over it, and keeps the card when typing over the break beside it", () => {
    const quoted = card("Quoted");
    const { editor } = mountDocument(`Opener\n${quoted}\nWhy`);
    typeAt(editor, "Opener\n".length, "x", "Opener\n".length + quoted.length);
    expect(editor.state.doc.toString()).toBe("Opener\nx\nWhy");

    const kept = mountDocument(`Intro\n${quoted}\nReply`);
    typeAt(kept.editor, "Intro".length, "x", "Intro\n".length);
    expect(kept.editor.state.doc.toString()).toBe(`Introx\n${quoted}\nReply`);
  });

  it("keeps a card whole when a compound edit touches it, and the rest of the edit lands", () => {
    const quoted = card("Quoted");
    const value = `Opener\n${quoted}\nWhy`;
    const { editor } = mountDocument(value);
    const start = value.indexOf(quoted);
    act(
      () =>
        void editor.dispatch({
          changes: [
            { from: start + 1, to: start + 3, insert: "" },
            { from: value.length, insert: "!" },
          ],
          userEvent: "input.type",
        }),
    );
    expect(editor.state.doc.toString()).toBe(`Opener\n${quoted}\nWhy!`);
  });

  it("keeps the text of an insert whose range ends inside a card", () => {
    const quoted = card("Quoted");
    const { target } = mountDocument(`Opener\n${quoted}\nWhy`);
    // The range stops two characters into the card: the edit keeps the card and its insert.
    act(() => insertComposerText(target(), "X", 0, "Opener\n".length + 2));
    expect(target().value).toBe(`X\n${quoted}\nWhy`);
  });

  it("removes a card with Backspace at the start of the line after it", () => {
    const value = `Opener\n${card("Quoted")}\nWhy`;
    const { content, editor } = mountDocument(value);
    caret(editor, value.indexOf("Why"));
    key(content, { key: "Backspace" });
    expect(editor.state.doc.toString()).toBe("Opener\nWhy");
  });

  it("removes a card with Delete at the end of the line before it", () => {
    const value = `Opener\n${card("Quoted")}\nWhy`;
    const { content, editor } = mountDocument(value);
    caret(editor, "Opener".length);
    key(content, { key: "Delete" });
    expect(editor.state.doc.toString()).toBe("Opener\nWhy");
  });

  it("removes a card with its X", () => {
    const value = `Opener\n${card("Quoted")}\nWhy`;
    const { container, target } = mountDocument(value);
    const remove = container.querySelector<HTMLButtonElement>('button[aria-label="Remove quote"]')!;
    act(() => void fireEvent.click(remove));
    expect(target().value).toBe("Opener\nWhy");
  });

  it("brings a removed card back with its text on undo, and removes it again on redo", () => {
    const quoted = card("Quoted");
    const value = `Opener\n${quoted}\nWhy`;
    const { content, editor } = mountDocument(value);
    caret(editor, value.indexOf("Why"));
    key(content, { key: "Backspace" });
    expect(editor.state.doc.toString()).toBe("Opener\nWhy");
    act(() => void undo(editor));
    expect(editor.state.doc.toString()).toBe(value);
    act(() => void redo(editor));
    expect(editor.state.doc.toString()).toBe("Opener\nWhy");
  });

  it("drops a card pasted through the composer's own insert, so no duplicate card appears", () => {
    const { editor, target } = mountDocument(`Opener\nWhy`);
    const at = "Opener\n".length;
    act(() => insertComposerText(target(), card("Pasted", "copy"), at, at));
    expect(editor.state.doc.toString()).toBe("Opener\nWhy");
  });

  it("turns cards into markdown quotes in the text copy, cut and drag take", () => {
    const quoted = card("one\ntwo");
    const value = `Opener\n${quoted}\nWhy`;
    const { editor } = mountDocument(value);
    // jsdom has no DOM selection for CodeMirror's copy handler, so read the filter it applies.
    const filters = editor.state.facet(EditorView.clipboardOutputFilter);
    const copied = filters.reduce((text, filter) => filter(text, editor.state), value);
    expect(copied).toBe("Opener\n> one\n> two\nWhy");
  });

  it("keeps the text after a removed seed in place, not below the next card", () => {
    const seed = card("Seed");
    const next = card("Next", "next");
    const value = `${seed}\nRest\n${next}\nMore`;
    const { content, editor, target } = mountDocument(value, { pinnedFirstCard: true });
    caret(editor, seed.length + 1);
    key(content, { key: "Backspace" });
    expect(target().value).toBe(`Rest\n${next}\nMore`);
  });

  it("reports the edit when the seed's X removes it, and keeps the text after it in place", () => {
    const seed = card("Seed");
    const next = card("Next", "next");
    const onChange = vi.fn();
    const { container, target } = mountDocument(`${seed}\nRest\n${next}\nMore`, {
      pinnedFirstCard: true,
      onChange,
    });
    const remove = container.querySelector<HTMLButtonElement>('button[aria-label="Remove quote"]')!;
    act(() => void fireEvent.click(remove));
    expect(target().value).toBe(`Rest\n${next}\nMore`);
    expect(onChange).toHaveBeenCalled();
  });

  it("keeps an unselected card when a selection that joins text onto it is deleted", () => {
    const quoted = card("Quoted");
    const { editor } = mountDocument(`Intro\n${quoted}\nReply`);
    // "tro\n" is the selection: deleting it joins "In" onto the card, which then stays on its own line.
    act(() => void editor.dispatch({ changes: { from: 2, to: 6 }, userEvent: "delete.backward" }));
    expect(editor.state.doc.toString()).toBe(`In\n${quoted}\nReply`);
  });

  it("removes adjacent cards one at a time, in the direction of the key", () => {
    const first = card("One", "one");
    const second = card("Two", "two");
    const value = `A\n${first}\n${second}\nB`;
    const { content, editor, target } = mountDocument(value);
    caret(editor, `A\n${first}\n`.length);
    key(content, { key: "Backspace" });
    expect(target().value).toBe(`A\n${second}\nB`);

    const again = mountDocument(value);
    caret(again.editor, `A\n${first}`.length);
    key(again.content, { key: "Delete" });
    expect(again.target().value).toBe(`A\n${first}\nB`);
  });

  it("follows a controlled value that removes a card from the middle, or swaps the cards", () => {
    const cards = [card("One", "one"), card("Two", "two"), card("Three", "three")];
    const { rerender, setTarget, target } = mountDocument(`A\n${cards.join("\n")}\nB`);
    const removedMiddle = `A\n${cards[0]}\n${cards[2]}\nB`;
    rerender(
      <CodeMirrorComposerInput
        aria-label="Message the agent"
        value={removedMiddle}
        ref={setTarget}
      />,
    );
    expect(target().value).toBe(removedMiddle);

    const swapped = `A\n${card("Four", "four")}\nB`;
    rerender(
      <CodeMirrorComposerInput aria-label="Message the agent" value={swapped} ref={setTarget} />,
    );
    expect(target().value).toBe(swapped);
  });

  it("keeps the seed card first: text typed above it lands below it", () => {
    const seed = card("Seed");
    const { editor, target } = mountDocument(`${seed}\nRest`, { pinnedFirstCard: true });
    typeAt(editor, 0, "x");
    expect(target().value).toBe(`${seed}\nxRest`);
    expect(editor.state.selection.main.head).toBe(seed.length + 2);
  });

  it("removes the seed card with its X and keeps the text after it", () => {
    const seed = card("Seed");
    const onChange = vi.fn();
    const { container, target } = mountDocument(`${seed}\nRest`, {
      pinnedFirstCard: true,
      onChange,
    });
    const remove = container.querySelector<HTMLButtonElement>('button[aria-label="Remove quote"]')!;
    act(() => void fireEvent.click(remove));
    expect(target().value).toBe("Rest");
  });
});
