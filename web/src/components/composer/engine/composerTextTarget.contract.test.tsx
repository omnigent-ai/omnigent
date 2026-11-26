// The ComposerTextTarget contract, run against every input engine: shared composer logic
// relies on these semantics whichever engine renders the draft.

import { act, render } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { insertComposerText } from "@/lib/composerText";
import { ComposerTextarea } from "../ChatComposer";
import { CodeMirrorComposerInput } from "./testing/codeMirrorComposer";
import { createFakeComposerSurface } from "./testing/fakeComposerSurface";
import {
  composerTargetElement,
  composerTargetOf,
  isComposerTargetFocused,
  type ComposerTextTarget,
} from "./composerInput";

interface Mounted {
  target: () => ComposerTextTarget;
  /** Draft changes the engine reported to the composer, in order. */
  changes: () => number;
  setValue: (value: string) => void;
}

interface MountOptions {
  /** Mount read-only, as a locked draft is. */
  locked?: boolean;
  /** Mount disabled, which also refuses edits. */
  disabled?: boolean;
}

function mountTextarea(
  initial: string,
  { locked = false, disabled = false }: MountOptions = {},
): Mounted {
  const onChange = vi.fn();
  const box: { set?: (value: string) => void; element?: HTMLTextAreaElement | null } = {};
  function Harness() {
    const [value, setValue] = useState(initial);
    box.set = setValue;
    return (
      <ComposerTextarea
        ref={(element) => {
          box.element = element;
        }}
        readOnly={locked}
        disabled={disabled}
        value={value}
        onChange={(event) => {
          onChange(event.currentTarget.value);
          setValue(event.currentTarget.value);
        }}
      />
    );
  }
  render(<Harness />);
  return {
    target: () => composerTargetOf(box.element)!,
    changes: () => onChange.mock.calls.length,
    setValue: (value) => act(() => box.set!(value)),
  };
}

function mountCodeMirror(
  initial: string,
  { locked = false, disabled = false }: MountOptions = {},
): Mounted {
  const onChange = vi.fn();
  const box: { set?: (value: string) => void; target?: ComposerTextTarget | null } = {};
  function Harness() {
    const [value, setValue] = useState(initial);
    box.set = setValue;
    return (
      <CodeMirrorComposerInput
        ref={(target) => {
          box.target = target;
        }}
        readOnly={locked}
        disabled={disabled}
        value={value}
        onChange={(event) => {
          onChange(event.currentTarget.value);
          setValue(event.currentTarget.value);
        }}
      />
    );
  }
  render(<Harness />);
  return {
    target: () => box.target!,
    changes: () => onChange.mock.calls.length,
    setValue: (value) => act(() => box.set!(value)),
  };
}

/** A surface has no React change events, so its edits are counted as they land. */
function mountFake(
  initial: string,
  { locked = false, disabled = false }: MountOptions = {},
): Mounted {
  const surface = createFakeComposerSurface(initial, { locked: locked || disabled });
  return {
    target: () => surface,
    changes: () => surface.inserts.length,
    setValue: (value) => {
      surface.value = value;
    },
  };
}

afterEach(() => {
  document.body.replaceChildren();
});

describe.each([
  ["textarea", mountTextarea],
  ["codemirror", mountCodeMirror],
  ["fake", mountFake],
])("ComposerTextTarget on the %s engine", (_engine, mount) => {
  it("is found from its focusable node", () => {
    const { target } = mount("draft");
    expect(composerTargetOf(composerTargetElement(target()))).toBe(target());
    expect(target().value).toBe("draft");
  });

  it("replaces the selection as one change", () => {
    const { target, changes } = mount("hello world");
    act(() => target().setSelectionRange(6, 11));
    act(() => insertComposerText(target(), "there"));
    expect(target().value).toBe("hello there");
    expect(changes()).toBe(1);
    expect(target().selectionStart).toBe("hello there".length);
  });

  it("moves the selection without taking focus", () => {
    const { target } = mount("one two three");
    act(() => target().setSelectionRange(4, 7));
    expect(isComposerTargetFocused(target())).toBe(false);
    expect([target().selectionStart, target().selectionEnd]).toEqual([4, 7]);
  });

  it("collapses reversed selection bounds to the end offset", () => {
    const { target } = mount("abcdef");
    act(() => target().setSelectionRange(5, 2));
    expect([target().selectionStart, target().selectionEnd]).toEqual([2, 2]);
  });

  it("keeps its selection across blur and focus", () => {
    const { target } = mount("one two three");
    act(() => target().focus());
    act(() => target().setSelectionRange(4, 7));
    act(() => target().blur());
    expect([target().selectionStart, target().selectionEnd]).toEqual([4, 7]);
    act(() => target().focus({ preventScroll: true }));
    expect(isComposerTargetFocused(target())).toBe(true);
    expect([target().selectionStart, target().selectionEnd]).toEqual([4, 7]);
  });
});

describe.each([
  ["textarea", mountTextarea],
  ["codemirror", mountCodeMirror],
  ["fake", mountFake],
])("ComposerTextTarget locked on the %s engine", (_engine, mount) => {
  it.each([
    ["read-only", { locked: true }],
    ["disabled", { disabled: true }],
  ] as const)(
    "refuses an insert into a %s draft and keeps its draft, selection and change count",
    (_kind, options) => {
      const { target, changes } = mount("draft", options);
      act(() => target().setSelectionRange(1, 3));

      expect(insertComposerText(target(), "x", 0, 0)).toBe(false);

      expect(target().value).toBe("draft");
      expect([target().selectionStart, target().selectionEnd]).toEqual([1, 3]);
      expect(changes()).toBe(0);
    },
  );
});

// The fake surface has no controlled-value sync, so only the rendered engines take a value.
describe.each([
  ["textarea", mountTextarea],
  ["codemirror", mountCodeMirror],
])("ComposerTextTarget controlled value on the %s engine", (_engine, mount) => {
  it("takes a controlled value without reporting it as a change", () => {
    const { target, changes, setValue } = mount("draft");
    setValue("");
    expect(target().value).toBe("");
    expect(changes()).toBe(0);
  });

  it("puts the caret at the end of a new value", () => {
    const { target, setValue } = mount("hello world");
    act(() => target().setSelectionRange(2, 2));
    setValue("hello there, friend");
    expect([target().selectionStart, target().selectionEnd]).toEqual([19, 19]);
  });
});
