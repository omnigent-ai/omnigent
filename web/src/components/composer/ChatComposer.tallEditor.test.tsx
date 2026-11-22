import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  composerHeightOf,
  resetComposerHeightsForTests,
  setComposerHeight,
} from "@/lib/composerHeightState";
import { insertComposerText } from "@/lib/composerText";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

import { ChatComposer } from "./ChatComposer";
import * as CodeMirrorModule from "./engine/codeMirrorComposerModule";
import {
  codeMirrorComposerLoad,
  loadCodeMirrorComposer,
  resetCodeMirrorComposerLoadForTests,
} from "./engine/codeMirrorComposerLoader";
import { composerTargetOf, type ComposerInputEngine } from "./engine/composerInput";

const KEYBOARD = { submitWithModEnter: false, preventsKeyboardSubmit: false } as const;
const LONG_PASTE = Array.from({ length: 20 }, (_, index) => `line ${index + 1}`).join("\n");

function Harness({
  engine = "codemirror",
  heightScope = "session:a",
  initial = "",
  quoted = false,
}: {
  engine?: ComposerInputEngine;
  /** Null renders the box with no height scope. */
  heightScope?: string | null;
  initial?: string;
  quoted?: boolean;
}) {
  const [value, setValue] = useState(initial);
  return (
    <ChatComposer
      engine={engine}
      heightScope={heightScope ?? undefined}
      keyboard={KEYBOARD}
      input={{
        "aria-label": "Message the agent",
        value,
        onChange: (event) => setValue(event.currentTarget.value),
      }}
      slots={quoted ? { inputPrefix: <blockquote>quoted reply</blockquote> } : undefined}
      actions={{ leading: null, trailing: null }}
    />
  );
}

/** Each input scrolls once it holds more than `visibleLines` of draft. */
function draftsOverflowPast(visibleLines: number) {
  vi.spyOn(Element.prototype, "scrollHeight", "get").mockImplementation(function (this: Element) {
    const lines = (this.textContent ?? "").split(/line \d+/).length - 1;
    return this.matches("textarea, .cm-scroller") && lines > visibleLines ? 500 : 0;
  });
}

const nextFrame = () =>
  new Promise<void>((resolve) => {
    requestAnimationFrame(() => resolve());
  });
const toggle = () => screen.queryByTestId("composer-height-toggle");
const scroller = () => document.querySelector<HTMLElement>(".cm-scroller");
const heightRows = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);

async function renderEditor(props: Parameters<typeof Harness>[0] = {}) {
  render(<Harness {...props} />);
  await waitFor(() => expect(screen.getByLabelText("Message the agent")).toHaveClass("cm-content"));
  return screen.getByLabelText("Message the agent");
}

beforeEach(async () => {
  resetCodeMirrorComposerLoadForTests(async () => CodeMirrorModule);
  loadCodeMirrorComposer();
  await waitFor(() => expect(codeMirrorComposerLoad().status).toBe("ready"));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  resetComposerHeightsForTests();
  resetCodeMirrorComposerLoadForTests();
  usePaletteCommandsStore.setState({ groups: {} });
});

describe("ChatComposer's tall editor", () => {
  it("offers the toggle once the draft overflows, and expands the editor to the tall height", async () => {
    draftsOverflowPast(3);
    const editor = await renderEditor();
    expect(toggle()).toBeNull();

    act(() => insertComposerText(composerTargetOf(editor)!, LONG_PASTE));
    await waitFor(() => expect(toggle()).toHaveAccessibleName("Expand message box"));
    fireEvent.click(toggle()!);

    expect(composerHeightOf("session:a")).toBe("expanded");
    expect(getComputedStyle(scroller()!).height).toBe("var(--composer-tall-height)");
    expect(screen.getByTestId("composer-height-toggle")).toHaveAccessibleName(
      "Collapse message box",
    );
    fireEvent.click(toggle()!);
    expect(composerHeightOf("session:a")).toBe("compact");
    expect(getComputedStyle(scroller()!).height).not.toBe("var(--composer-tall-height)");
  });

  it("shows the height its session had, and another session's box starts compact", async () => {
    setComposerHeight("session:a", "expanded");
    await renderEditor();
    expect(toggle()).toHaveAccessibleName("Collapse message box");
    cleanup();

    await renderEditor({ heightScope: "session:b" });
    expect(toggle()).toBeNull();
    expect(getComputedStyle(scroller()!).height).not.toBe("var(--composer-tall-height)");
  });

  it("makes the quote area the tall scroller when quote blocks sit above the draft", async () => {
    setComposerHeight("session:a", "expanded");
    await renderEditor({ quoted: true });
    const area = document.querySelector("[data-composer-input-area]")!;
    expect(area).toHaveClass("h-(--composer-tall-height)", "overflow-y-auto");
    expect(area).not.toHaveClass("max-h-[320px]");
    expect(getComputedStyle(scroller()!).height).not.toBe("var(--composer-tall-height)");
  });

  it.each([
    ["the textarea", { engine: "textarea" as const }],
    ["a box without a height scope", { heightScope: null }],
  ])("never expands %s, however long the draft", async (_name, props) => {
    draftsOverflowPast(3);
    setComposerHeight("session:a", "expanded");
    render(<Harness {...props} initial={LONG_PASTE} />);
    // Give a deferred measure its frame.
    await act(nextFrame);
    expect(toggle()).toBeNull();
    expect(heightRows()).toEqual([]);
    expect(document.querySelector("[data-composer-card]")).not.toHaveClass(
      "[--composer-tall-height:min(55dvh,36rem)]",
    );
  });
});

describe("turning the CodeMirror input off", () => {
  it("drops the tall controls at once, even while an IME composition holds the editor", async () => {
    setComposerHeight("session:a", "expanded");
    const view = render(<Harness />);
    await waitFor(() => expect(toggle()).toHaveAccessibleName("Collapse message box"));
    fireEvent.compositionStart(screen.getByLabelText("Message the agent"));

    view.rerender(<Harness engine="textarea" />);
    // The editor stays until the composition ends; the tall controls don't.
    expect(screen.getByLabelText("Message the agent")).toHaveClass("cm-content");
    expect(toggle()).toBeNull();
    expect(heightRows()).toEqual([]);
    expect(getComputedStyle(scroller()!).height).not.toBe("var(--composer-tall-height)");
  });
});

describe("a long paste into the tall editor's box", () => {
  /** Paste `text` into `editor`, landing it in the draft as the editor would. */
  function paste(editor: HTMLElement, text: string, lands = true) {
    act(() => {
      const event = new Event("paste", { bubbles: true, cancelable: true });
      Object.defineProperty(event, "clipboardData", {
        value: { getData: (type: string) => (type === "text/plain" ? text : "") },
      });
      // The editor takes the paste itself; this test lands the text as it would.
      event.preventDefault();
      editor.dispatchEvent(event);
      const target = composerTargetOf(editor)!;
      if (lands) insertComposerText(target, text, target.value.length, target.value.length);
    });
  }

  it("expands an empty box once the text lands", async () => {
    const editor = await renderEditor();
    paste(editor, LONG_PASTE);
    expect(composerHeightOf("session:a")).toBe("expanded");
  });

  it.each([
    ["a short paste", "", "a few words"],
    ["a paste into a draft that has text", "draft so far ", LONG_PASTE],
  ])("leaves the box compact for %s", async (_name, initial, text) => {
    const editor = await renderEditor({ initial });
    paste(editor, text);
    expect(composerHeightOf("session:a")).toBe("compact");
  });

  it("doesn't expand another session's box with a paste made in this one", async () => {
    const view = render(<Harness />);
    await waitFor(() =>
      expect(screen.getByLabelText("Message the agent")).toHaveClass("cm-content"),
    );
    paste(screen.getByLabelText("Message the agent"), LONG_PASTE, false);
    // The session changes before the text lands, and the next session's draft grows long.
    view.rerender(<Harness heightScope="session:b" />);
    act(() =>
      insertComposerText(composerTargetOf(screen.getByLabelText("Message the agent"))!, LONG_PASTE),
    );
    expect(composerHeightOf("session:b")).toBe("compact");
    expect(composerHeightOf("session:a")).toBe("compact");
  });

  it("leaves the height alone once the box can't expand, even with a paste pending", async () => {
    const view = render(<Harness />);
    await waitFor(() =>
      expect(screen.getByLabelText("Message the agent")).toHaveClass("cm-content"),
    );
    paste(screen.getByLabelText("Message the agent"), LONG_PASTE, false);
    view.rerender(<Harness heightScope={null} />);
    act(() =>
      insertComposerText(composerTargetOf(screen.getByLabelText("Message the agent"))!, LONG_PASTE),
    );
    expect(composerHeightOf("session:a")).toBe("compact");
    expect(composerHeightOf("")).toBe("compact");
  });

  it("leaves the box compact when the paste never lands, as one that becomes an attachment", async () => {
    const editor = await renderEditor();
    paste(editor, LONG_PASTE, false);
    await act(async () => {
      await nextFrame();
      await nextFrame();
    });
    act(() => insertComposerText(composerTargetOf(editor)!, LONG_PASTE));
    expect(composerHeightOf("session:a")).toBe("compact");
  });
});
