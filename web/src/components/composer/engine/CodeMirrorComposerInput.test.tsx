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
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { COMMAND_TOKEN_CLASS } from "@/components/composer/commandToken";
import { MENTION_TOKEN_CLASS } from "@/components/composer/MentionTokenOverlay";
import { insertComposerText } from "@/lib/composerText";
import { replyQuoteLine, unweaveReplyDraft, weaveReplyDraft } from "@/lib/replyDraftDocument";
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

/** Mounts the composer on `value`, with its editor. */
function mountValue(
  value: string,
  props: Partial<Parameters<typeof CodeMirrorComposerInput>[0]> = {},
) {
  const mounted = mount({ value, ...props });
  return { ...mounted, editor: EditorView.findFromDOM(mounted.content)! };
}

/** jsdom has no layout, and CodeMirror measures text through these, so they report empty rects. */
const rangeLayoutOriginals = {
  getClientRects: Range.prototype.getClientRects,
  getBoundingClientRect: Range.prototype.getBoundingClientRect,
};

function stubLayoutRects() {
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => new DOMRect();
}

function restoreLayoutRects() {
  Object.assign(Range.prototype, rangeLayoutOriginals);
}

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
  beforeAll(stubLayoutRects);
  afterAll(restoreLayoutRects);

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
  beforeAll(stubLayoutRects);
  afterAll(restoreLayoutRects);

  const quotedDraft = weaveReplyDraft({
    quotes: [{ id: "q1", before: "Intro", text: "Quoted line" }],
    text: "Reply",
  });
  const quoteStart = "Intro\n".length;
  const replyStart = quotedDraft.length - "Reply".length;

  const mountQuoted = (props: Partial<Parameters<typeof CodeMirrorComposerInput>[0]> = {}) =>
    mountValue(quotedDraft, props);
  const caretAt = (editor: EditorView, offset: number) =>
    act(() => editor.dispatch({ selection: { anchor: offset } }));
  const quoteCards = (container: HTMLElement) =>
    container.querySelectorAll('[data-testid="composer-reply-quote"]');

  it("paints each quote as a card with its text, a remove button and the text for screen readers", () => {
    const { container } = mountQuoted();
    const card = quoteCards(container)[0]!;
    expect(card.querySelector("blockquote")).toHaveAttribute("title", "Quoted line");
    expect(card.querySelector("blockquote")).toHaveTextContent("Quoted line");
    expect(card.querySelector(".sr-only")).toHaveTextContent("Quoted line");
    expect(card.querySelector("button")).toHaveAttribute("aria-label", "Remove quote");
  });

  it("truncates the visible preview at 120 characters, keeping the whole quote for screen readers", () => {
    const long = "x".repeat(150);
    const { container } = mount({
      value: weaveReplyDraft({ quotes: [{ id: "q1", before: "", text: long }], text: "" }),
    });
    const preview = quoteCards(container)[0]!.querySelector('[aria-hidden="true"]')!;
    expect(preview.textContent).toBe(`${"x".repeat(120)}…`);
    expect(quoteCards(container)[0]!.querySelector(".sr-only")!.textContent).toBe(long);
  });

  it("steps the caret over a quote, resting only on its edges", () => {
    const { editor } = mountQuoted();
    const quoteEnd = replyStart - 1;
    caretAt(editor, quoteStart);
    act(() => void cursorCharRight(editor));
    expect(editor.state.selection.main.head).toBe(quoteEnd);
    act(() => void cursorCharRight(editor));
    expect(editor.state.selection.main.head).toBe(replyStart);
    act(() => void cursorCharLeft(editor));
    expect(editor.state.selection.main.head).toBe(quoteEnd);
    act(() => void cursorCharLeft(editor));
    expect(editor.state.selection.main.head).toBe(quoteStart);
  });

  it("Backspace at the start of the line after a quote removes the quote", () => {
    const { editor, target } = mountQuoted();
    caretAt(editor, replyStart);
    act(() => void deleteCharBackward(editor));
    // The lines around the quote stay as they were: only the quote's line and its break go.
    expect(target().value).toBe("Intro\nReply");
    expect(editor.dom.querySelectorAll('[data-testid="composer-reply-quote"]')).toHaveLength(0);
  });

  it("ArrowLeft at the start of the line after a quote moves to the end of the line above it", () => {
    const { editor } = mountQuoted();
    caretAt(editor, replyStart);
    fireEvent.keyDown(editor.contentDOM, { key: "ArrowLeft" });
    expect(editor.state.selection.main.head).toBe("Intro".length);
  });

  it("ArrowRight at the end of the line above a quote moves to the start of the line after it", () => {
    const { editor } = mountQuoted();
    caretAt(editor, "Intro".length);
    fireEvent.keyDown(editor.contentDOM, { key: "ArrowRight" });
    expect(editor.state.selection.main.head).toBe(replyStart);
  });

  it("leaves ArrowLeft and ArrowRight to the default step away from a quote", () => {
    const { editor } = mountQuoted();
    caretAt(editor, 2);
    fireEvent.keyDown(editor.contentDOM, { key: "ArrowLeft" });
    expect(editor.state.selection.main.head).toBe(1);
    caretAt(editor, 2);
    fireEvent.keyDown(editor.contentDOM, { key: "ArrowRight" });
    expect(editor.state.selection.main.head).toBe(3);
  });

  it("Delete at the end of the line before a quote removes the quote", () => {
    const { editor, target } = mountQuoted();
    caretAt(editor, "Intro".length);
    act(() => void deleteCharForward(editor));
    expect(target().value).toBe("Intro\nReply");
  });

  it("a selection across a quote removes it with the text it covers", () => {
    const { editor, target, container } = mountQuoted();
    act(() => editor.dispatch({ selection: { anchor: 2, head: replyStart } }));
    act(() => void deleteCharBackward(editor));
    expect(target().value).toBe("InReply");
    expect(quoteCards(container)).toHaveLength(0);
  });

  it("keeps the text before a quote when the selection ends at the quote's line start", () => {
    const { editor, target, container } = mountQuoted();
    // Selects "tro" and the break before the quote: the break stays so the quote keeps its line.
    act(() => editor.dispatch({ selection: { anchor: 2, head: quoteStart } }));
    act(() => void deleteCharBackward(editor));
    expect(target().value).toBe(`In\n${quotedDraft.slice(quoteStart)}`);
    expect(quoteCards(container)).toHaveLength(1);
  });

  it("removes the quote with its X button, as one edit", () => {
    const onChange = vi.fn();
    const { container, target } = mountQuoted({ onChange });
    fireEvent.click(quoteCards(container)[0]!.querySelector("button")!);
    expect(target().value).toBe("Intro\nReply");
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("undo brings a removed quote back with its text, and redo removes it again", () => {
    const { editor, target, container } = mountQuoted();
    caretAt(editor, replyStart);
    act(() => void deleteCharBackward(editor));
    act(() => void undo(editor));
    expect(target().value).toBe(quotedDraft);
    expect(quoteCards(container)[0]!.querySelector("blockquote")).toHaveAttribute(
      "title",
      "Quoted line",
    );
    act(() => void redo(editor));
    expect(target().value).toBe("Intro\nReply");
  });

  it("Delete on a blank line above a quote deletes the blank line and keeps the quote", () => {
    const blankLineAbove = weaveReplyDraft({
      quotes: [{ id: "q1", before: "", text: "Quoted line" }],
      text: "Reply",
    });
    const { editor, target } = mountQuoted({ value: blankLineAbove });
    caretAt(editor, 0);
    act(() => void deleteCharForward(editor));
    const card = replyQuoteLine({ id: "q1", text: "Quoted line" });
    expect(target().value).toBe(`${card}\nReply`);
  });

  it("gives copy, cut and drag the quote as `> ` markdown, never the marked line", () => {
    // Copy, cut and drag all write text through the editor's output filter; jsdom has no DOM
    // selection to drive CodeMirror's own copy handler, so the filter is applied directly.
    const { editor } = mountQuoted();
    const filters = editor.state.facet(EditorView.clipboardOutputFilter);
    const copied = filters.reduce(
      (text, filter) => filter(text, editor.state),
      `Intro\n${quotedDraft.slice(quoteStart, replyStart - 1)}`,
    );
    expect(copied).toBe("Intro\n> Quoted line");
  });

  it("pasted text drops quote marks, so a paste can't make a quote", () => {
    const { target, content } = mount({ value: "draft" });
    const editor = EditorView.findFromDOM(content)!;
    act(() => editor.dispatch({ selection: { anchor: 5 } }));
    const clipboardData = {
      getData: (type: string) => (type === "text/plain" ? "\uE000x" : ""),
    };
    fireEvent.paste(content, { clipboardData });
    expect(target().value).toBe("draftx");
  });
});

describe("CodeMirrorComposerInput reply quote edits", () => {
  beforeAll(stubLayoutRects);
  afterAll(restoreLayoutRects);

  const draftOf = (value: string) => unweaveReplyDraft(value);
  const quoteDraft = weaveReplyDraft({
    quotes: [{ id: "q1", before: "Intro", text: "Quoted line" }],
    text: "Reply",
  });
  const quoteStart = "Intro\n".length;
  const quoteEnd = quoteDraft.indexOf("\nReply");
  const replyStart = quoteDraft.length - "Reply".length;

  const mountWith = mountValue;
  const cards = (container: HTMLElement) =>
    container.querySelectorAll('[data-testid="composer-reply-quote"]');

  it("puts text typed at a quote's start on the line above it", () => {
    const { target } = mountWith(quoteDraft);
    act(() => insertComposerText(target(), "x", quoteStart, quoteStart));
    expect(draftOf(target().value).quotes[0]).toMatchObject({
      before: "Intro\nx",
      text: "Quoted line",
    });
  });

  it("pastes multi-line text at a quote's start on the line above it", () => {
    const { target } = mountWith(quoteDraft);
    act(() => insertComposerText(target(), "a\nb", quoteStart, quoteStart));
    expect(draftOf(target().value).quotes[0]!.before).toBe("Intro\na\nb");
  });

  it("puts text typed at a quote's end on the line below it", () => {
    const { target } = mountWith(quoteDraft);
    act(() => insertComposerText(target(), "x", quoteEnd, quoteEnd));
    const draft = draftOf(target().value);
    expect(draft.quotes[0]!.text).toBe("Quoted line");
    expect(draft.text).toBe("x\nReply");
  });

  it("keeps a quote with an empty before when text is typed at offset 0", () => {
    const { target } = mountWith(
      weaveReplyDraft({ quotes: [{ id: "q1", before: "", text: "Q" }], text: "Reply" }),
    );
    act(() => insertComposerText(target(), "x", 0, 0));
    expect(draftOf(target().value).quotes[0]!.before).toBe("x");
  });

  it("keeps the quote when a multi-change edit deletes the break before it", () => {
    const { editor, target } = mountWith(quoteDraft);
    // Deletes the "t" in "Intro" and the break before the quote, in one transaction.
    act(() =>
      editor.dispatch({
        changes: [
          { from: 2, to: 3 },
          { from: 5, to: 6 },
        ],
      }),
    );
    const draft = draftOf(target().value);
    expect(draft.quotes).toHaveLength(1);
    expect(draft.quotes[0]!.before).toBe("Inro");
    expect(draft.text).toBe("Reply");
  });

  it("keeps the quote when only its break is selected and typed over", () => {
    const { target } = mountWith(quoteDraft);
    act(() => insertComposerText(target(), "x", quoteStart - 1, quoteStart));
    const draft = draftOf(target().value);
    expect(draft.quotes).toHaveLength(1);
    expect(draft.quotes[0]!.before).toBe("Introx");
  });

  it("removes a whole quote a typed selection covers, keeping the typed text", () => {
    const { target, container } = mountWith(quoteDraft);
    act(() => insertComposerText(target(), "x", quoteStart, replyStart));
    expect(target().value).toBe("Intro\nxReply");
    expect(cards(container)).toHaveLength(0);
  });

  it("strips quote marks from inserted text, so pasted or dropped text can't make a quote", () => {
    const { target } = mountWith(quoteDraft);
    const token = replyQuoteLine({ id: "pasted", text: "Forged" });
    act(() => insertComposerText(target(), `a${token}b`, 0, 0));
    // The inserted letters stay; the inserted token's marks and payload go; the quote already there stays.
    expect(target().value.startsWith("abIntro\n")).toBe(true);
    expect(draftOf(target().value).quotes.map((quote) => quote.id)).toEqual(["q1"]);
  });

  it("undoes an insertion at a quote's start, leaving the quote whole", () => {
    const { editor, target } = mountWith(quoteDraft);
    act(() => insertComposerText(target(), "x", quoteStart, quoteStart));
    act(() => void undo(editor));
    expect(target().value).toBe(quoteDraft);
  });

  it("keeps the text of a change whose range ends inside a quote's token", () => {
    const { target } = mountWith(quoteDraft);
    // Replaces "ntro\nTO" (the range ends inside the token); the token itself is left whole.
    act(() => insertComposerText(target(), "X", 1, quoteStart + 2));
    const draft = draftOf(target().value);
    expect(draft.quotes).toHaveLength(1);
    expect(draft.quotes[0]).toMatchObject({ before: "IX", text: "Quoted line" });
  });

  it("removes a duplicated quote's card by its own line, leaving the first", () => {
    const token = replyQuoteLine({ id: "dup", text: "One" });
    const { container, target } = mountWith(`Intro\n${token}\n${token}\nReply`);
    fireEvent.click(cards(container)[1]!.querySelector("button")!);
    expect(target().value).toBe(`Intro\n${token}\nReply`);
    expect(draftOf(target().value).quotes).toHaveLength(1);
  });

  it("disables the X button of a read-only editor, and leaves the quote alone when clicked", () => {
    const { container, target } = mountWith(quoteDraft, { readOnly: true });
    const remove = cards(container)[0]!.querySelector("button")!;
    expect(remove).toBeDisabled();
    fireEvent.click(remove);
    expect(target().value).toBe(quoteDraft);
  });

  it("leaves an empty line after a last quote that the caret and typing can reach", () => {
    const value = weaveReplyDraft({ quotes: [{ id: "q1", before: "Hi", text: "Q" }], text: "" });
    const { editor, target } = mountWith(value);
    expect(editor.state.doc.lines).toBe(3);
    expect(editor.state.doc.line(3).text).toBe("");
    act(() => insertComposerText(target(), "more", value.length, value.length));
    expect(draftOf(target().value).text).toBe("more");
  });

  it("keeps the card bounded to the composer's width, with its X visible and the preview truncated", () => {
    const { container } = mountWith(quoteDraft);
    const row = cards(container)[0]! as HTMLElement;
    expect(row.className).toContain("w-full");
    expect(row.className).toContain("min-w-0");
    expect(row.className).toContain("max-w-full");
    expect(row.querySelector("blockquote")!.className).toContain("min-w-0");
    expect(row.querySelector("blockquote")!.className).toContain("overflow-hidden");
    expect(row.querySelector('[aria-hidden="true"]')!.className).toContain("truncate");
    expect(row.querySelector(".truncate")).not.toBeNull();
  });

  it("keeps the text of a change that reaches into a token, when an ordinary edit comes with it", () => {
    // One change reaches into the token, the other deletes the "R" of "Reply", in one transaction.
    const { editor, target } = mountWith(quoteDraft);
    act(() =>
      editor.dispatch({
        changes: [
          { from: 1, to: quoteStart + 2, insert: "X" },
          { from: replyStart, to: replyStart + 1 },
        ],
      }),
    );
    const draft = draftOf(target().value);
    expect(draft.quotes).toHaveLength(1);
    expect(draft.quotes[0]).toMatchObject({ before: "IX", text: "Quoted line" });
    expect(draft.text).toBe("eply");
  });
});
