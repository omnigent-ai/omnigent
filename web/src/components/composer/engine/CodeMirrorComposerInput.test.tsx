import { redo, undo } from "@codemirror/commands";
import { EditorView } from "@codemirror/view";
import { act, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { insertComposerText } from "@/lib/composerText";
import { CodeMirrorComposerInput } from "./testing/codeMirrorComposer";
import { COMPOSER_EDITING_KEYMAP } from "./CodeMirrorComposerInput";
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

  it("refuses an insert into a read-only draft and leaves it unchanged", () => {
    const { target } = mount({ value: "draft", readOnly: true });
    expect(insertComposerText(target(), "x", 0, 0)).toBe(false);
    expect(target().value).toBe("draft");
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
    const { content, target, rerender, setTarget } = mount({ value: "a", onChange });
    rerender(
      <CodeMirrorComposerInput
        aria-label="Message the agent"
        value="ab"
        onChange={onChange}
        ref={setTarget}
      />,
    );
    expect(target().value).toBe("ab");
    expect(onChange).not.toHaveBeenCalled();

    act(() => void undo(EditorView.findFromDOM(content)!));
    expect(target().value).toBe("ab");
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

  it("restores the controlled value when a parent rejects an edit", () => {
    const onChange = vi.fn();
    const { target, rerender, setTarget } = mount({ value: "draft", onChange });
    act(() => insertComposerText(target(), "x", 5, 5));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(target().value).toBe("draftx");

    // The parent re-renders with the same value, as after an unrelated state change.
    rerender(
      <CodeMirrorComposerInput
        aria-label="Message the agent"
        value="draft"
        onChange={onChange}
        ref={setTarget}
      />,
    );
    expect(target().value).toBe("draft");
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
