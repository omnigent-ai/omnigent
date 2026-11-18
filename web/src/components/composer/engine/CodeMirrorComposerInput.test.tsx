import {
  cursorCharLeft,
  cursorCharRight,
  deleteCharBackward,
  redo,
  selectCharRight,
  undo,
} from "@codemirror/commands";
import { EditorView } from "@codemirror/view";
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { COMMAND_TOKEN_CLASS } from "@/components/composer/commandToken";
import { MENTION_TOKEN_CLASS } from "@/components/composer/MentionTokenOverlay";
import { insertComposerText } from "@/lib/composerText";
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

  it("marks a token typed after mount, and drops it once the text no longer matches", () => {
    const { container, target } = mount({ mentionTokens: ["@a.ts"] });
    expect(tokenMarks(container)).toEqual([]);

    act(() => {
      insertComposerText(target(), "@a.ts", 0, 0);
    });
    expect(tokenMarks(container)).toHaveLength(1);

    act(() => {
      insertComposerText(target(), "x", "@a.ts".length, "@a.ts".length);
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
});
