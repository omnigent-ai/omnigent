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

function mountTextarea(initial: string): Mounted {
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

function mountCodeMirror(initial: string): Mounted {
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
function mountFake(initial: string): Mounted {
  const surface = createFakeComposerSurface(initial);
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

  it("takes a controlled value without reporting it as a change", () => {
    const { target, changes, setValue } = mount("draft");
    setValue("");
    expect(target().value).toBe("");
    expect(changes()).toBe(0);
  });

  it("moves the selection without taking focus", () => {
    const { target } = mount("one two three");
    act(() => target().setSelectionRange(4, 7));
    expect(isComposerTargetFocused(target())).toBe(false);
    expect([target().selectionStart, target().selectionEnd]).toEqual([4, 7]);
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
