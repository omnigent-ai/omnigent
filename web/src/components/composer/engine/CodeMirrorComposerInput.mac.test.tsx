// The full CodeMirror composer as macOS runs it. CodeMirror reads the platform once, when it
// loads, to pick its macOS bindings; vi.hoisted sets the platform before any import runs.

import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { useRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { COMPOSER_EDITING_KEYMAP } from "./CodeMirrorComposerInput";
import { READLINE_BINDINGS } from "./codeMirrorReadline";
import { CodeMirrorComposerInput } from "./testing/codeMirrorComposer";
import { insertComposerText } from "@/lib/composerText";
import { quoteLine } from "@/lib/replyDraftDocument";
import { useMentionBrowser, type MentionBrowser } from "@/hooks/useMentionBrowser";
import type { MentionState } from "@/lib/composerMentions";
import type { ComposerTextTarget } from "./composerInput";

vi.hoisted(() => {
  const mac = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131";
  Object.defineProperty(navigator, "platform", { configurable: true, get: () => "MacIntel" });
  Object.defineProperty(navigator, "userAgent", { configurable: true, get: () => mac });
});

function mount(
  value: string,
  caret: number,
  mentionTokens?: readonly string[],
  replyQuotes = false,
) {
  const box: { target: ComposerTextTarget | null } = { target: null };
  const { container } = render(
    <CodeMirrorComposerInput
      aria-label="Message the agent"
      defaultValue={value}
      mentionTokens={mentionTokens}
      replyQuotes={replyQuotes}
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

/** The editor wired to the mention hook, with `token` already tagged in the draft. */
function mountTaggedDraft(draft: string, token: string) {
  const hooks: { current: MentionBrowser | null } = { current: null };
  const box: { target: ComposerTextTarget | null } = { target: null };
  function TaggedDraft() {
    const [text, setText] = useState(draft);
    const [mention, setMention] = useState<MentionState | null>(null);
    const textareaRef = useRef<ComposerTextTarget | null>(null);
    const browser = useMentionBrowser({
      mention,
      setMention,
      mentionEntries: [],
      text,
      setText,
      textareaRef,
      initialMentions: { files: [{ path: token.slice(1), isDir: false, token }], sessions: [] },
    });
    hooks.current = browser;
    return (
      <CodeMirrorComposerInput
        aria-label="Message the agent"
        value={text}
        mentionTokens={browser.mentionTokens}
        ref={(target) => {
          textareaRef.current = target;
          box.target = target;
        }}
        onChange={(event) => {
          browser.noteTextChange(event);
          setText(event.currentTarget.value);
        }}
      />
    );
  }
  const { container } = render(<TaggedDraft />);
  const content = container.querySelector<HTMLElement>(".cm-content")!;
  act(() => box.target!.setSelectionRange(draft.length, draft.length));
  const press = (init: KeyboardEventInit) => fireEvent.keyDown(content, init);
  return { target: () => box.target!, press, tags: () => hooks.current!.mentionTokens };
}

afterEach(cleanup);

describe("CodeMirror composer keys on macOS", () => {
  it("installs no Control+Option binding, on any platform's key", () => {
    for (const binding of [...READLINE_BINDINGS, ...COMPOSER_EDITING_KEYMAP]) {
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

  it("kills a multi-word mention whole on ⌃W, so ⌃Y brings all of it back", () => {
    const draft = "ask @Alpha planning ";
    const { target, press } = mount(draft, draft.length, ["@Alpha planning"]);

    press({ key: "w", code: "KeyW", ctrlKey: true });
    expect(target().value).toBe("ask ");
    press({ key: "y", code: "KeyY", ctrlKey: true });
    expect(target().value).toBe(draft);
  });

  it.each([
    // Caret at the end: ⌃W kills the pill and its trailing space; ⌃U kills the whole line.
    { key: "w", caret: 20, killed: "ask " },
    { key: "u", caret: 20, killed: "" },
    // Caret before the pill: ⌃K kills to the line end, which takes the pill whole.
    { key: "k", caret: 4, killed: "ask " },
  ])("restores a pill's tag when ⌘Z undoes a ⌃$key kill of it", ({ key, caret, killed }) => {
    const draft = "ask @Alpha planning ";
    const { target, press, tags } = mountTaggedDraft(draft, "@Alpha planning");
    act(() => target().setSelectionRange(caret, caret));
    expect(tags()).toEqual(["@Alpha planning"]);

    press({ key, code: `Key${key.toUpperCase()}`, ctrlKey: true });
    expect(target().value).toBe(killed);
    expect(tags()).toEqual([]);
    press({ key: "z", code: "KeyZ", metaKey: true });
    expect(target().value).toBe(draft);
    expect(tags()).toEqual(["@Alpha planning"]);
  });

  it("yanks a killed pill back as literal text, without its tag", () => {
    const draft = "ask @Alpha planning ";
    const { target, press, tags } = mountTaggedDraft(draft, "@Alpha planning");
    press({ key: "w", code: "KeyW", ctrlKey: true });
    press({ key: "y", code: "KeyY", ctrlKey: true });
    expect(target().value).toBe(draft);
    expect(tags()).toEqual([]);
  });

  it("yanks both words after two ⌃W kills in a row", () => {
    const { target, press } = mount("git push origin", 15);

    press({ key: "w", code: "KeyW", ctrlKey: true });
    press({ key: "w", code: "KeyW", ctrlKey: true });
    press({ key: "y", code: "KeyY", ctrlKey: true });

    expect(target().value).toBe("git push origin");
  });

  it("yanks only the newest kill once the caret has moved between kills", () => {
    const { target, press } = mount("git push origin", 15);

    press({ key: "w", code: "KeyW", ctrlKey: true });
    act(() => target().setSelectionRange(8, 8));
    press({ key: "w", code: "KeyW", ctrlKey: true });
    press({ key: "y", code: "KeyY", ctrlKey: true });

    expect(target().value).toBe("git push ");
  });
});

describe("readline kills beside a reply card", () => {
  const value = `Intro\n${quoteLine("a", "Alpha")}\nReply`;
  const afterCard = value.length - "Reply".length;

  it.each([
    ["⌃U at the start of the line after it", afterCard, "u"],
    ["⌃W at the start of the line after it", afterCard, "w"],
    ["⌃K at the end of the line before it", "Intro".length, "k"],
  ])("stops %s", (_where, caret, key) => {
    const { target, press } = mount(value, caret, undefined, true);
    press({ key, code: `Key${key.toUpperCase()}`, ctrlKey: true });
    expect(target().value).toBe(value);
  });

  it("kills a selection across a card whole, which ⌃Y yanks back as a quote and undo restores", () => {
    const { target, press } = mount(value, 0, undefined, true);
    act(() => target().setSelectionRange(2, value.length - 2));
    press({ key: "w", code: "KeyW", ctrlKey: true });
    expect(target().value).toBe("Inly");
    press({ key: "y", code: "KeyY", ctrlKey: true });
    expect(target().value).toBe("Intro\n> Alpha\nReply");
    press({ key: "z", code: "KeyZ", metaKey: true });
    expect(target().value).toBe("Inly");
    press({ key: "z", code: "KeyZ", metaKey: true });
    expect(target().value).toBe(value);
  });

  it("kills up to a card but not across it", () => {
    const { target, press } = mount(value, value.length, undefined, true);
    press({ key: "u", code: "KeyU", ctrlKey: true });
    expect(target().value).toBe(value.slice(0, afterCard));
    press({ key: "w", code: "KeyW", ctrlKey: true });
    expect(target().value).toBe(value.slice(0, afterCard));
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
      // Mid-text, so the deletions would change the draft if the lock didn't stop them.
      act(() => box.target!.setSelectionRange(5, 5));

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
    // Editable, so this ⌃W kills "draft" and puts it in the ring for the yank below.
    fireEvent.keyDown(content, { key: "w", code: "KeyW", ctrlKey: true });
    expect(box.target!.value).toBe("keep this ");
    onChange.mockClear();

    rerender(view(true));
    fireEvent.keyDown(content, { key: "y", code: "KeyY", ctrlKey: true });
    fireEvent.keyDown(content, { key: "w", code: "KeyW", ctrlKey: true });

    expect(box.target!.value).toBe("keep this ");
    expect(onChange).not.toHaveBeenCalled();
  });
});
