import { act, renderHook } from "@testing-library/react";
import { createRef, StrictMode, useState, type ReactNode } from "react";
import { describe, expect, it } from "vitest";
import { useDictationInsert } from "./useDictationInsert";

function renderDictation(
  initial = "",
  wrapper?: ({ children }: { children: ReactNode }) => ReactNode,
) {
  const textarea = document.createElement("textarea");
  textarea.value = initial;
  const textareaRef = createRef<HTMLTextAreaElement>();
  textareaRef.current = textarea;

  const hook = renderHook(
    () => {
      const [value, setValue] = useState(initial);
      textarea.value = value;
      const dictation = useDictationInsert(value, setValue, textareaRef);
      return {
        value,
        textarea,
        setRaw: (next: string) => {
          dictation.reset();
          setValue(() => next);
        },
        setRawUnsafe: (next: string) => setValue(() => next),
        edit: (next: string) => {
          dictation.reconcileUserEdit(next);
          setValue(() => next);
        },
        ...dictation,
      };
    },
    wrapper ? { wrapper } : undefined,
  );
  return hook;
}

function select(result: ReturnType<typeof renderDictation>["result"], start: number, end = start) {
  result.current.textarea.setSelectionRange(start, end);
}

describe("useDictationInsert", () => {
  it("captures a clamped selection and replaces it with the first interim", () => {
    const { result } = renderDictation("hello cruel world");
    select(result, 6, 11);
    act(() => result.current.begin());
    act(() => result.current.replaceInterim("kind"));
    expect(result.current.value).toBe("hello kind world");
  });

  it("revises its exact interim region in the middle of a draft", () => {
    const { result } = renderDictation("alpha omega");
    select(result, 5);
    act(() => result.current.begin());
    act(() => result.current.replaceInterim("bravo"));
    expect(result.current.value).toBe("alpha bravo omega");
    act(() => result.current.replaceInterim("bravo charlie"));
    expect(result.current.value).toBe("alpha bravo charlie omega");
    act(() => result.current.replaceInterim("b"));
    expect(result.current.value).toBe("alpha b omega");
  });

  it("final replaces the exact interim and later finals continue at its caret", () => {
    const { result } = renderDictation("start finish");
    select(result, 5);
    act(() => result.current.begin());
    act(() => result.current.replaceInterim("one tw"));
    act(() => result.current.appendFinal("One two."));
    act(() => result.current.appendFinal("Three."));
    expect(result.current.value).toBe("start One two. Three. finish");
  });

  it("uses the caret when begin was not called and avoids doubled separators", () => {
    const { result } = renderDictation("left  right");
    select(result, 5);
    act(() => result.current.appendFinal("middle"));
    expect(result.current.value).toBe("left middle right");
  });

  it("restores the original selection and whitespace when an interim clears", () => {
    const { result } = renderDictation("left  old  right");
    select(result, 6, 9);
    act(() => result.current.begin());
    act(() => result.current.replaceInterim("new"));
    expect(result.current.value).toBe("left  new  right");
    act(() => result.current.replaceInterim(""));
    expect(result.current.value).toBe("left  old  right");
  });

  it("removes only separators it inserted when clearing a caret interim", () => {
    const { result } = renderDictation("left right");
    select(result, 4);
    act(() => result.current.begin());
    act(() => result.current.replaceInterim("middle"));
    expect(result.current.value).toBe("left middle right");
    act(() => result.current.replaceInterim(""));
    expect(result.current.value).toBe("left right");
  });

  it("never deletes user text when the owned region changed", () => {
    const { result } = renderDictation("alpha omega");
    select(result, 5);
    act(() => result.current.begin());
    act(() => result.current.replaceInterim("partial"));
    act(() => {
      result.current.textarea.setSelectionRange(
        result.current.value.length,
        result.current.value.length,
      );
      result.current.setRawUnsafe("alpha user-edited omega");
    });
    act(() => result.current.appendFinal("Final."));
    expect(result.current.value).toBe("alpha user-edited omega Final.");
  });

  it("rebases ownership when the user edits before the interim", () => {
    const { result } = renderDictation("alpha omega");
    select(result, 5);
    act(() => result.current.begin());
    act(() => result.current.replaceInterim("partial"));
    act(() => result.current.edit(`note ${result.current.value}`));
    act(() => result.current.appendFinal("Final."));
    expect(result.current.value).toBe("note alpha Final. omega");
  });

  it("keeps ownership when the user edits after the interim", () => {
    const { result } = renderDictation("alpha omega");
    select(result, 5);
    act(() => result.current.begin());
    act(() => result.current.replaceInterim("partial"));
    act(() => result.current.edit(`${result.current.value} note`));
    act(() => result.current.appendFinal("Final."));
    expect(result.current.value).toBe("alpha Final. omega note");
  });

  it("keeps ownership when the user types at the displayed interim caret", async () => {
    const { result } = renderDictation("alpha omega");
    select(result, 5);
    act(() => result.current.begin());
    act(() => result.current.replaceInterim("partial"));
    await act(async () => Promise.resolve());
    const caret = result.current.textarea.selectionStart;
    const next = result.current.value.slice(0, caret) + " note" + result.current.value.slice(caret);
    act(() => result.current.edit(next));
    act(() => result.current.appendFinal("Final."));
    expect(result.current.value).toBe("alpha Final. note omega");
  });

  it("invalidates ownership when the user edits inside the interim", () => {
    const { result } = renderDictation("alpha omega");
    select(result, 5);
    act(() => result.current.begin());
    act(() => result.current.replaceInterim("partial"));
    act(() => result.current.edit("alpha user-edited omega"));
    select(result, result.current.value.length);
    act(() => result.current.appendFinal("Final."));
    expect(result.current.value).toBe("alpha user-edited omega Final.");
  });

  it("reset prevents stale selection from affecting an external replacement", () => {
    const { result } = renderDictation("old draft");
    select(result, 0, 3);
    act(() => result.current.begin());
    act(() => result.current.setRaw("replacement"));
    select(result, 11);
    act(() => result.current.appendFinal("spoken"));
    expect(result.current.value).toBe("replacement spoken");
  });

  it("rebases the pending selection when the user edits before the first transcript", () => {
    const { result } = renderDictation("alpha omega");
    select(result, 6, 11);
    act(() => result.current.begin());
    act(() => result.current.edit("note alpha omega"));
    act(() => result.current.appendFinal("Final."));
    expect(result.current.value).toBe("note alpha Final.");
  });

  it("is StrictMode-safe", () => {
    const strict = ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>;
    const { result } = renderDictation("draft end", strict);
    select(result, 5);
    act(() => result.current.begin());
    act(() => result.current.replaceInterim("one"));
    act(() => result.current.replaceInterim("one two"));
    act(() => result.current.appendFinal("One, two."));
    expect(result.current.value).toBe("draft One, two. end");
  });
});
