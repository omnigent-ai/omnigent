// The full CodeMirror composer as macOS runs it. CodeMirror reads the platform once, when it
// loads, to pick its macOS bindings; vi.hoisted sets the platform before any import runs.

import { act, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { COMPOSER_EDITING_KEYMAP } from "./CodeMirrorComposerInput";
import { CodeMirrorComposerInput } from "./testing/codeMirrorComposer";
import { insertComposerText } from "@/lib/composerText";
import type { ComposerTextTarget } from "./composerInput";

vi.hoisted(() => {
  const mac = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131";
  Object.defineProperty(navigator, "platform", { configurable: true, get: () => "MacIntel" });
  Object.defineProperty(navigator, "userAgent", { configurable: true, get: () => mac });
});

function mount(value: string, caret: number) {
  const box: { target: ComposerTextTarget | null } = { target: null };
  const { container } = render(
    <CodeMirrorComposerInput
      aria-label="Message the agent"
      defaultValue={value}
      ref={(target) => {
        box.target = target;
      }}
    />,
  );
  const content = container.querySelector<HTMLElement>(".cm-content")!;
  act(() => {
    box.target!.focus();
    box.target!.setSelectionRange(caret, caret);
  });
  const press = (init: KeyboardEventInit) => fireEvent.keyDown(content, init);
  return { target: () => box.target!, press };
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("CodeMirror composer keys on macOS", () => {
  it("installs no Control+Option binding, on any platform's key", () => {
    for (const binding of COMPOSER_EDITING_KEYMAP) {
      for (const key of [binding.key, binding.mac, binding.win, binding.linux]) {
        if (key) expect(key).not.toMatch(/Ctrl-.*Alt-|Alt-.*Ctrl-/);
      }
    }
  });

  it.each(["h", "d", "w", "k", "u", "Backspace", "ArrowLeft"])(
    "leaves VoiceOver's ⌃⌥%s to VoiceOver and the draft untouched",
    (key) => {
      const { target, press } = mount("hello world", 11);

      const notClaimed = press({ key, ctrlKey: true, altKey: true });

      expect(notClaimed).toBe(true);
      expect(target().value).toBe("hello world");
    },
  );

  it("still deletes the previous shell word on ⌃W through the full editor", () => {
    const { target, press } = mount("git push origin", 15);

    const notClaimed = press({ key: "w", code: "KeyW", ctrlKey: true });

    expect(notClaimed).toBe(false);
    expect(target().value).toBe("git push ");
  });

  it("leaves Option letters to type their characters", () => {
    const { target, press } = mount("draft", 5);

    expect(press({ key: "∫", code: "KeyB", altKey: true })).toBe(true);
    expect(target().value).toBe("draft");
  });
});

describe("a locked CodeMirror composer", () => {
  it.each([
    ["read-only", { readOnly: true }],
    ["disabled", { disabled: true }],
  ] as const)(
    "keeps a %s draft through readline kills, yanks and shared inserts",
    (_name, lock) => {
      const onChange = vi.fn();
      const box: { target: ComposerTextTarget | null } = { target: null };
      const { container } = render(
        <CodeMirrorComposerInput
          {...lock}
          defaultValue="keep this draft"
          onChange={onChange}
          ref={(target) => {
            box.target = target;
          }}
        />,
      );
      const content = container.querySelector<HTMLElement>(".cm-content")!;
      act(() => box.target!.setSelectionRange(15, 15));

      for (const key of ["w", "k", "u", "y", "d", "h"]) {
        fireEvent.keyDown(content, { key, code: `Key${key.toUpperCase()}`, ctrlKey: true });
      }
      act(() => insertComposerText(box.target!, "pasted"));

      expect(box.target!.value).toBe("keep this draft");
      expect(onChange).not.toHaveBeenCalled();
    },
  );

  it("stops editing at once when it turns disabled while focused", () => {
    const onChange = vi.fn();
    const box: { target: ComposerTextTarget | null } = { target: null };
    const view = (disabled: boolean) => (
      <CodeMirrorComposerInput
        disabled={disabled}
        defaultValue="keep this draft"
        onChange={onChange}
        ref={(target) => {
          box.target = target;
        }}
      />
    );
    const { container, rerender } = render(view(false));
    const content = container.querySelector<HTMLElement>(".cm-content")!;
    act(() => {
      box.target!.focus();
      box.target!.setSelectionRange(15, 15);
    });

    rerender(view(true));
    fireEvent.keyDown(content, { key: "w", code: "KeyW", ctrlKey: true });

    expect(box.target!.value).toBe("keep this draft");
    expect(onChange).not.toHaveBeenCalled();
  });
});
