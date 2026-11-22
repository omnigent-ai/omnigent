import { act, renderHook, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as CodeMirrorModule from "@/components/composer/engine/codeMirrorComposerModule";
import {
  codeMirrorComposerLoad,
  loadCodeMirrorComposer,
  resetCodeMirrorComposerLoadForTests,
} from "@/components/composer/engine/codeMirrorComposerLoader";
import type {
  ComposerKeyboardEvent,
  ComposerPasteEvent,
  ComposerTextTarget,
} from "@/components/composer/engine/composerInput";
import {
  createFakeComposerSurface,
  type FakeComposerSurface,
} from "@/components/composer/engine/testing/fakeComposerSurface";
import { isMacPlatform } from "@/lib/hotkeys";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { useComposerPastedText } from "./useComposerPastedText";
import { loadPastedTextLedger, resetPastedTextLedgerForTests } from "./usePastedTextAttachments";

const LARGE = Array.from({ length: 60 }, (_, index) => `log line ${index}`).join("\n");

const inputs: FakeComposerSurface[] = [];
beforeEach(async () => {
  resetCodeMirrorComposerLoadForTests(async () => CodeMirrorModule);
  loadCodeMirrorComposer();
  await waitFor(() => expect(codeMirrorComposerLoad().status).toBe("ready"));
  await loadPastedTextLedger();
});
afterEach(() => {
  for (const input of inputs.splice(0)) input.unregister();
  resetCodeMirrorComposerLoadForTests();
  resetPastedTextLedgerForTests();
  usePaletteCommandsStore.setState({ groups: {} });
});

function liveInput(value = "") {
  const input = createFakeComposerSurface(value);
  inputs.push(input);
  return input;
}

/** An owner whose draft is the one live input, and whose attachments are kept in state. */
function renderPaste(input: FakeComposerSurface, { locked = false } = {}) {
  const onOtherPaste = vi.fn();
  // Stable, as an owner's is until its draft changes.
  const valueOf = () => input.value;
  const hook = renderHook(
    (props: { locked: boolean }) => {
      const [files, setFiles] = useState<File[]>([]);
      const pasted = useComposerPastedText({
        commandSource: "test-pasted-text",
        draftKey: "draft-1",
        files,
        valueOf,
        attach: (file) => {
          setFiles((current) => [...current, file]);
          return true;
        },
        removeFile: (file) => setFiles((current) => current.filter((each) => each !== file)),
        writeInput: () => {},
        targetOf: () => input,
        revealInput: () => {},
        locked: props.locked,
        onOtherPaste,
      });
      return { pasted, files };
    },
    { initialProps: { locked } },
  );
  return { ...hook, onOtherPaste };
}

const pasteEvent = (
  target: ComposerTextTarget,
  text: string,
  files: File[] = [],
): ComposerPasteEvent & { preventDefault: ReturnType<typeof vi.fn<() => void>> } => ({
  clipboardData: {
    getData: (format: string) => (format === "text/plain" ? text : ""),
    files,
    items: files.map((file) => ({ kind: "file", type: file.type, getAsFile: () => file })),
    types: files.length > 0 ? ["Files"] : ["text/plain"],
  } as unknown as DataTransfer,
  currentTarget: target,
  preventDefault: vi.fn<() => void>(),
});

const undoKey = (target: ComposerTextTarget) =>
  ({
    key: "z",
    code: "KeyZ",
    altKey: false,
    // The page's Mod: ⌘ on macOS, Ctrl elsewhere, as jsdom reports.
    ctrlKey: !isMacPlatform(),
    metaKey: isMacPlatform(),
    shiftKey: false,
    repeat: false,
    defaultPrevented: false,
    nativeEvent: new KeyboardEvent("keydown"),
    currentTarget: target,
    preventDefault: vi.fn(),
    stopPropagation: vi.fn(),
  }) as unknown as ComposerKeyboardEvent & { preventDefault: ReturnType<typeof vi.fn> };

describe("useComposerPastedText", () => {
  it("turns a large paste on CodeMirror into an attachment, and ⌘Z types it back", () => {
    const input = liveInput("see: ");
    input.setSelectionRange(5, 5);
    const { result, onOtherPaste } = renderPaste(input);
    const event = pasteEvent(input, LARGE);

    act(() => result.current.pasted.onPaste(event));
    expect(event.preventDefault).toHaveBeenCalled();
    expect(onOtherPaste).not.toHaveBeenCalled();
    const [file] = result.current.files;
    expect(file?.name).toBe("pasted-text-1.txt");
    expect(result.current.pasted.actionFor(file!)).toMatchObject({
      label: "Insert as text",
      detail: "60 lines",
    });

    const undo = undoKey(input);
    act(() => {
      expect(result.current.pasted.onKeyDown(undo)).toBe(true);
    });
    expect(undo.preventDefault).toHaveBeenCalled();
    expect(input.value).toBe(`see: ${LARGE}`);
    expect(result.current.files).toEqual([]);
  });

  it("leaves the textarea's paste, a paste with files, and a short paste to the composer", () => {
    const input = liveInput();
    const { result, onOtherPaste } = renderPaste(input);
    const textarea = document.createElement("textarea");
    const file = new File(["notes"], "notes.txt", { type: "text/plain" });
    const pastes = [pasteEvent(textarea, LARGE), pasteEvent(input, LARGE, [file])];
    pastes.push(pasteEvent(input, "short"));

    for (const event of pastes) act(() => result.current.pasted.onPaste(event));

    expect(onOtherPaste.mock.calls.map(([event]) => event)).toEqual(pastes);
    expect(result.current.files).toEqual([]);
  });

  it("refuses every paste while locked, without the composer's own handling", () => {
    const input = liveInput();
    const { result, onOtherPaste } = renderPaste(input, { locked: true });
    const event = pasteEvent(input, LARGE);
    act(() => result.current.pasted.onPaste(event));
    expect(event.preventDefault).toHaveBeenCalled();
    expect(onOtherPaste).not.toHaveBeenCalled();
    expect(result.current.files).toEqual([]);
  });

  it("leaves ⌘Z alone when no paste just became an attachment", () => {
    const input = liveInput();
    const { result } = renderPaste(input);
    const undo = undoKey(input);
    expect(result.current.pasted.onKeyDown(undo)).toBe(false);
    expect(undo.preventDefault).not.toHaveBeenCalled();
  });

  it("offers each paste in ⌘K while the draft can restore it, and withdraws them when locked", () => {
    const input = liveInput();
    const { result, rerender } = renderPaste(input);
    act(() => result.current.pasted.onPaste(pasteEvent(input, LARGE)));
    act(() => result.current.pasted.onPaste(pasteEvent(input, `${LARGE}\nmore`)));

    const commands = () => usePaletteCommandsStore.getState().groups["test-pasted-text"];
    expect(commands()?.heading).toBe("Message");
    expect(commands()?.commands.map((command) => command.label)).toEqual([
      "Insert pasted-text-1.txt as Text",
      "Insert pasted-text-2.txt as Text",
    ]);
    act(() => commands()!.commands[0]!.run());
    expect(result.current.files.map((file) => file.name)).toEqual(["pasted-text-2.txt"]);

    rerender({ locked: true });
    expect(commands()).toBeUndefined();
    expect(result.current.pasted.actionFor(result.current.files[0]!)).toBeUndefined();
  });
});
