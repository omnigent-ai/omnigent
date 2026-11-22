import { act, renderHook, waitFor } from "@testing-library/react";
import { useCallback, useState } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import * as CodeMirrorModule from "@/components/composer/engine/codeMirrorComposerModule";
import {
  codeMirrorComposerLoad,
  loadCodeMirrorComposer,
  resetCodeMirrorComposerLoadForTests,
} from "@/components/composer/engine/codeMirrorComposerLoader";
import {
  COMPOSER_INPUT_ID_ATTRIBUTE,
  COMPOSER_TAIL_INPUT_ID,
} from "@/components/composer/engine/composerInput";
import {
  createFakeComposerSurface,
  type FakeComposerSurface,
} from "@/components/composer/engine/testing/fakeComposerSurface";
import {
  loadPastedTextLedger,
  resetPastedTextLedgerForTests,
  usePastedTextAttachments,
} from "./usePastedTextAttachments";

const LARGE = Array.from({ length: 60 }, (_, index) => `log line ${index}`).join("\n");
const LARGE_TWO = `${LARGE}\nsecond`;
const clipboard = (text: string) =>
  ({ getData: (format: string) => (format === "text/plain" ? text : "") }) as DataTransfer;

/** An owner with a tail and optional quote inputs, live CodeMirror-like inputs for each. */
function renderOwner({
  tail = "",
  quotes = {} as Record<string, string>,
  accepts = true,
  freshValueOf = false,
}: {
  tail?: string;
  quotes?: Record<string, string>;
  accepts?: boolean;
  /** A reader that's new on every render, so the ledger syncs before the attachment lands. */
  freshValueOf?: boolean;
} = {}) {
  const revealed: string[] = [];
  const inputs = new Map<string, FakeComposerSurface>();
  const hook = renderHook(
    ({ draftKey, locked = false }: { draftKey: string; locked?: boolean }) => {
      const [values, setValues] = useState<Record<string, string>>({
        [COMPOSER_TAIL_INPUT_ID]: tail,
        ...quotes,
      });
      const [files, setFiles] = useState<File[]>([]);
      const stableValueOf = useCallback((inputId: string) => values[inputId], [values]);
      const valueOf = freshValueOf ? (inputId: string) => values[inputId] : stableValueOf;
      const pasted = usePastedTextAttachments({
        draftKey,
        files,
        valueOf,
        attach: (file) => {
          if (!accepts) return false;
          setFiles((current) => [...current, file]);
          return true;
        },
        removeFile: (file) => setFiles((current) => current.filter((each) => each !== file)),
        targetOf: (inputId) => (inputId in values ? (inputs.get(inputId) ?? null) : null),
        writeInput: (inputId, value) => setValues((current) => ({ ...current, [inputId]: value })),
        revealInput: (inputId) => revealed.push(inputId),
        locked,
      });
      return { pasted, values, setValues, files, setFiles };
    },
    { initialProps: { draftKey: "session-a" } as { draftKey: string; locked?: boolean } },
  );
  /** The live input for `inputId`, whose edits reach the owner as typing would. */
  const input = (inputId: string, selection?: [number, number]): FakeComposerSurface => {
    const surface = createFakeComposerSurface(hook.result.current.values[inputId]);
    surface.element.setAttribute(COMPOSER_INPUT_ID_ATTRIBUTE, inputId);
    if (selection) surface.setSelectionRange(...selection);
    const insertText = surface.insertText;
    surface.insertText = (text, start, end) => {
      const inserted = insertText(text, start, end);
      if (inserted) {
        hook.result.current.setValues((current) => ({ ...current, [inputId]: surface.value }));
      }
      return inserted;
    };
    inputs.set(inputId, surface);
    return surface;
  };
  const paste = (inputId: string, text: string, selection?: [number, number]) =>
    act(
      () => void hook.result.current.pasted.offerPaste(input(inputId, selection), clipboard(text)),
    );
  /** The owner changes an input's text, which a controlled input then shows. */
  const edit = (inputId: string, value: string) =>
    act(() => {
      hook.result.current.setValues((current) => ({ ...current, [inputId]: value }));
      const surface = inputs.get(inputId);
      if (surface) surface.value = value;
    });
  return { ...hook, input, inputs, paste, edit, revealed };
}

beforeEach(async () => {
  resetCodeMirrorComposerLoadForTests(async () => CodeMirrorModule);
  loadCodeMirrorComposer();
  await waitFor(() => expect(codeMirrorComposerLoad().status).toBe("ready"));
  await loadPastedTextLedger();
});
afterEach(() => {
  resetCodeMirrorComposerLoadForTests();
  resetPastedTextLedgerForTests();
  document.body.replaceChildren();
});

describe("usePastedTextAttachments", () => {
  it("leaves a paste under the threshold to the input", () => {
    const owner = renderOwner();
    const target = owner.input(COMPOSER_TAIL_INPUT_ID);
    expect(owner.result.current.pasted.offerPaste(target, clipboard("short"))).toBe(false);
    expect(owner.result.current.files).toEqual([]);
  });

  it("reports nothing before the CodeMirror chunk has loaded", () => {
    resetCodeMirrorComposerLoadForTests();
    const owner = renderOwner();
    const target = owner.input(COMPOSER_TAIL_INPUT_ID);
    expect(owner.result.current.pasted.offerPaste(target, clipboard(LARGE))).toBe(false);
    expect(owner.result.current.pasted.hint).toBeNull();
  });

  it("loads the ledger once the editor has, leaving pastes to the input until then", async () => {
    resetPastedTextLedgerForTests();
    const owner = renderOwner();
    const target = owner.input(COMPOSER_TAIL_INPUT_ID);
    expect(owner.result.current.pasted.offerPaste(target, clipboard(LARGE))).toBe(false);

    await waitFor(() =>
      expect(owner.result.current.pasted.offerPaste(target, clipboard(LARGE))).toBe(true),
    );
    expect(owner.result.current.files).toHaveLength(1);
  });

  it("attaches a large paste in place of the selection, and ⌘Z types it back undoably", async () => {
    const owner = renderOwner({ tail: "see the error below" });
    owner.paste(COMPOSER_TAIL_INPUT_ID, LARGE, [8, 13]);

    const [file] = owner.result.current.files;
    expect(await file.text()).toBe(LARGE);
    expect(owner.result.current.values.tail).toBe("see the  below");
    expect(owner.result.current.pasted.hint).toEqual({ file, lines: 60, undoable: true });

    act(() => {
      expect(owner.result.current.pasted.undoLatestPaste()).toBe(true);
    });
    const tail = owner.inputs.get(COMPOSER_TAIL_INPUT_ID)!;
    // Through the live input: one typed insert, caret after it.
    expect(tail.inserts.at(-1)).toEqual({ text: LARGE, start: 8, end: 8 });
    expect(tail.selectionStart).toBe(8 + LARGE.length);
    expect(owner.result.current.values.tail).toBe(`see the ${LARGE} below`);
    expect(owner.result.current.files).toEqual([]);
    expect(owner.result.current.pasted.hint).toBeNull();
  });

  it("keeps a just-accepted paste's chip and ⌘Z through a render before its file lands", () => {
    const owner = renderOwner({ tail: "see: ", freshValueOf: true });
    owner.paste(COMPOSER_TAIL_INPUT_ID, LARGE, [5, 5]);
    const [file] = owner.result.current.files;
    expect(owner.result.current.pasted.hint).toEqual({ file, lines: 60, undoable: true });

    act(() => {
      expect(owner.result.current.pasted.undoLatestPaste()).toBe(true);
    });
    expect(owner.result.current.values.tail).toBe(`see: ${LARGE}`);
  });

  it("consumes nothing when the attachment is rejected", () => {
    const owner = renderOwner({ tail: "keep this", accepts: false });
    owner.paste(COMPOSER_TAIL_INPUT_ID, LARGE, [0, 4]);
    expect(owner.result.current.values.tail).toBe("keep this");
    expect(owner.result.current.files).toEqual([]);
    expect(owner.result.current.pasted.hint).toBeNull();
  });

  it("ends ⌘Z on anything the owner reports, but keeps Insert as text", () => {
    const owner = renderOwner({ tail: "before after" });
    owner.paste(COMPOSER_TAIL_INPUT_ID, LARGE, [7, 7]);
    const [file] = owner.result.current.files;

    // A selection move, an edit elsewhere, an engine switch or a removed quote.
    act(() => owner.result.current.pasted.endUndo());

    expect(owner.result.current.pasted.hint?.undoable).toBe(false);
    act(() => {
      expect(owner.result.current.pasted.undoLatestPaste()).toBe(false);
    });
    act(() => void owner.result.current.pasted.insertAsText(file));
    expect(owner.result.current.values.tail).toBe(`before ${LARGE}after`);
  });

  it("follows later edits in the paste's input, which also end ⌘Z", () => {
    const owner = renderOwner({ tail: "before after" });
    owner.paste(COMPOSER_TAIL_INPUT_ID, LARGE, [7, 7]);
    const [file] = owner.result.current.files;

    owner.edit(COMPOSER_TAIL_INPUT_ID, "so, before after");

    expect(owner.result.current.pasted.hint?.undoable).toBe(false);
    act(() => void owner.result.current.pasted.insertAsText(file));
    expect(owner.result.current.values.tail).toBe(`so, before ${LARGE}after`);
  });

  it("carries an earlier paste through a later paste's replaced selection", () => {
    const owner = renderOwner({ tail: "0123456789ABC" });
    owner.paste(COMPOSER_TAIL_INPUT_ID, LARGE, [10, 10]);
    owner.paste(COMPOSER_TAIL_INPUT_ID, LARGE_TWO, [0, 5]);
    const [first] = owner.result.current.files;
    expect(owner.result.current.values.tail).toBe("56789ABC");

    act(() => void owner.result.current.pasted.insertAsText(first));

    expect(owner.result.current.values.tail).toBe(`56789${LARGE}ABC`);
  });

  it("restores any one paste on its own, keeping the others attached", () => {
    const owner = renderOwner({ tail: "ab" });
    owner.paste(COMPOSER_TAIL_INPUT_ID, LARGE, [1, 1]);
    owner.paste(COMPOSER_TAIL_INPUT_ID, LARGE_TWO, [2, 2]);
    const [first, second] = owner.result.current.files;
    expect(owner.result.current.pasted.chips.map((chip) => chip.file)).toEqual([first, second]);

    act(() => void owner.result.current.pasted.insertAsText(first));

    expect(owner.result.current.files).toEqual([second]);
    expect(owner.result.current.values.tail).toBe(`a${LARGE}b`);
    expect(owner.result.current.pasted.chips.map((chip) => chip.file)).toEqual([second]);
  });

  it("keeps two pastes made at the same place in order, whichever is restored first", () => {
    const owner = renderOwner({ tail: "see: " });
    owner.paste(COMPOSER_TAIL_INPUT_ID, LARGE, [5, 5]);
    owner.paste(COMPOSER_TAIL_INPUT_ID, LARGE_TWO, [5, 5]);
    const [first, second] = owner.result.current.files;

    act(() => void owner.result.current.pasted.insertAsText(first));
    act(() => void owner.result.current.pasted.insertAsText(second));

    expect(owner.result.current.values.tail).toBe(`see: ${LARGE}${LARGE_TWO}`);
  });

  it("forgets a paste whose chip was removed or sent", () => {
    const owner = renderOwner({ tail: "draft" });
    owner.paste(COMPOSER_TAIL_INPUT_ID, LARGE);
    const [file] = owner.result.current.files;

    act(() => owner.result.current.setFiles([]));

    expect(owner.result.current.pasted.hint).toBeNull();
    act(() => {
      expect(owner.result.current.pasted.insertAsText(file)).toBe(false);
    });
    expect(owner.result.current.values.tail).toBe("draft");
  });

  it("sends a paste to the end of the tail once its reply quote is gone", () => {
    const owner = renderOwner({ tail: "tail", quotes: { q1: "quote" } });
    owner.input(COMPOSER_TAIL_INPUT_ID);
    owner.paste("q1", LARGE, [2, 2]);
    const [file] = owner.result.current.files;

    act(() =>
      owner.result.current.setValues((current) => {
        const { q1: _removed, ...rest } = current;
        return rest;
      }),
    );
    act(() => void owner.result.current.pasted.insertAsText(file));

    expect(owner.result.current.values.tail).toBe(`tail${LARGE}`);
  });

  it("never deletes text when the draft shrank below a paste's offset", () => {
    const owner = renderOwner({ tail: "a long draft here" });
    owner.paste(COMPOSER_TAIL_INPUT_ID, LARGE, [17, 17]);
    const [file] = owner.result.current.files;

    owner.edit(COMPOSER_TAIL_INPUT_ID, "new");
    act(() => void owner.result.current.pasted.insertAsText(file));

    expect(owner.result.current.values.tail).toBe(`new${LARGE}`);
  });

  it("keeps the attachment when the draft is locked or the input refuses the restore", () => {
    const owner = renderOwner({ tail: "keep" });
    owner.paste(COMPOSER_TAIL_INPUT_ID, LARGE);
    const [file] = owner.result.current.files;

    owner.rerender({ draftKey: "session-a", locked: true });
    expect(owner.result.current.pasted.canRestore).toBe(false);
    act(() => {
      expect(owner.result.current.pasted.insertAsText(file)).toBe(false);
    });
    expect(owner.result.current.files).toEqual([file]);

    // Unlocked again, but the input itself still refuses the edit.
    owner.rerender({ draftKey: "session-a", locked: false });
    owner.inputs.set(COMPOSER_TAIL_INPUT_ID, createFakeComposerSurface("keep", { locked: true }));
    act(() => {
      expect(owner.result.current.pasted.insertAsText(file)).toBe(false);
    });
    expect(owner.result.current.files).toEqual([file]);
    expect(owner.result.current.values.tail).toBe("keep");
  });

  it("shows a hidden quote's input first, then restores through it", () => {
    const owner = renderOwner({ tail: "tail", quotes: { q1: "" } });
    owner.paste("q1", LARGE);
    const [file] = owner.result.current.files;
    // The quote's input unmounts once it's empty and inactive.
    owner.inputs.delete("q1");

    act(() => {
      expect(owner.result.current.pasted.insertAsText(file)).toBe(true);
    });
    expect(owner.revealed).toEqual(["q1"]);
    expect(owner.result.current.files).toEqual([file]);

    // The owner shows it again; the next render finishes through the live input.
    const shown = owner.input("q1");
    owner.rerender({ draftKey: "session-a" });

    expect(shown.inserts.at(-1)).toMatchObject({ text: LARGE, start: 0, end: 0 });
    expect(owner.result.current.files).toEqual([]);
  });

  it("forgets every paste when the session changes", () => {
    const owner = renderOwner({ tail: "draft" });
    owner.paste(COMPOSER_TAIL_INPUT_ID, LARGE);
    expect(owner.result.current.pasted.hint).not.toBeNull();

    owner.rerender({ draftKey: "session-b" });

    expect(owner.result.current.pasted.hint).toBeNull();
    act(() => {
      expect(owner.result.current.pasted.undoLatestPaste()).toBe(false);
    });
  });
});
