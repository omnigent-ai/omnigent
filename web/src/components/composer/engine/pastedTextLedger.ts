// The large-paste ledger, loaded on its own once the CodeMirror editor has, since only that
// engine turns a paste into an attachment. It remembers each paste by its File and the input it
// landed in, with a plain-text offset that follows later edits. It lives with the composer's
// owner, so it survives an engine switch, and it only ever inserts, so a stale offset can't
// delete anyone's text.

import type { ComposerTextTarget } from "./composerInput";
import {
  exceedsPasteThreshold,
  insertAtOffset,
  lineCount,
  mapOffsetThroughEdit,
  pastedPlainText,
  pastedTextFile,
} from "./pasteToAttachment";

/** The owner's draft and attachments, read fresh on every call. */
export interface PastedTextContext {
  files: readonly File[];
  /** The text of one input ("tail" or a reply quote's id), or undefined once it's gone. */
  valueOf: (inputId: string) => string | undefined;
  /** Attach `file` if it passes validation; false leaves the draft as it was. */
  attach: (file: File) => boolean;
  removeFile: (file: File) => void;
  /** The live input for `inputId`, so a restore is a real edit: undoable, caret after it. */
  targetOf: (inputId: string) => ComposerTextTarget | null;
  /** Replace one input's text, as the owner's own edits do; for an input not on screen. */
  writeInput: (inputId: string, value: string) => void;
  /** Show an existing input that isn't on screen, such as an empty reply quote's. */
  revealInput: (inputId: string) => void;
  /** The draft can't be edited now (read-only or disabled); restores wait until it can. */
  locked: boolean;
}

/** What the ledger takes from the app, handed in by its owner's hook. */
export interface PastedTextEnvironment {
  tailInputId: string;
  /** The input id of `target` ("tail" or a reply quote's). */
  inputIdOf: (target: ComposerTextTarget) => string | null;
  /**
   * Replace `[start, end)` of `target` as its own undo step; false when the input is locked
   * and nothing changed.
   */
  insertText: (target: ComposerTextTarget, text: string, start: number, end: number) => boolean;
}

/** A pasted attachment the draft can still take back as text. */
export interface PastedTextChip {
  file: File;
  lines: number;
}

export interface PastedTextSnapshot {
  /** Every paste still attached, oldest first. */
  chips: readonly PastedTextChip[];
  /** ⌘Z right now turns the newest paste back into text. */
  undoable: boolean;
}

interface PastedText {
  file: File;
  text: string;
  inputId: string;
  offset: number;
}

export interface PastedTextLedger {
  snapshot: () => PastedTextSnapshot;
  subscribe: (listener: () => void) => () => void;
  /**
   * Take a paste into `target` as an attachment when its text is large. Returns whether it
   * handled the paste, accepted or not, so the caller claims the event.
   */
  offerPaste: (
    target: ComposerTextTarget,
    data: DataTransfer,
    context: PastedTextContext,
  ) => boolean;
  insertAsText: (file: File, context: PastedTextContext) => boolean;
  undoLatest: (context: PastedTextContext) => boolean;
  /** Anything since the newest paste ends ⌘Z's restore; Insert as text keeps working. */
  endUndo: () => void;
  /** Carry offsets through the owner's edits, and forget pastes whose chip is gone. */
  sync: (context: PastedTextContext) => void;
  /** Finish a restore that waited for its input to appear; call after each render. */
  completePendingRestore: (context: PastedTextContext) => void;
  /** A new draft: forget every paste, and every draft text remembered for them. */
  reset: () => void;
  /** Whether any draft text is still remembered, which a reset or send must clear. */
  holdsDraftText: () => boolean;
}

export function createPastedTextLedger(environment: PastedTextEnvironment): PastedTextLedger {
  let pastes: PastedText[] = [];
  let undoable = false;
  let sequence = 0;
  // A restore whose input existed but wasn't mounted, waiting for the owner to show it.
  let pendingRestore: File | null = null;
  let snapshot: PastedTextSnapshot = { chips: [], undoable: false };
  // Each input's text as the offsets were last mapped against.
  const seen = new Map<string, string>();
  // Pastes whose file the owner's attachments have shown; only those can be removed from it.
  // A paste accepted a moment ago may not be in the owner's files yet.
  const landed = new WeakSet<File>();
  const listeners = new Set<() => void>();

  const publish = () => {
    snapshot = {
      chips: pastes.map((paste) => ({ file: paste.file, lines: lineCount(paste.text) })),
      undoable,
    };
    for (const listener of listeners) listener();
  };
  const remember = (inputId: string, value: string | undefined) => {
    if (value !== undefined) seen.set(inputId, value);
  };
  /** Every paste with its offset carried through edits since the last mapping. */
  const mapped = (context: PastedTextContext) =>
    pastes.map((paste) => {
      const value = context.valueOf(paste.inputId);
      const before = seen.get(paste.inputId);
      if (value === undefined || before === undefined || before === value) return paste;
      return { ...paste, offset: mapOffsetThroughEdit(paste.offset, before, value) };
    });

  /** A paste whose chip the owner had and has since removed or sent. */
  const isGone = (file: File, context: PastedTextContext) =>
    landed.has(file) && !context.files.includes(file);

  const sync = (context: PastedTextContext) => {
    if (pastes.length === 0) return;
    for (const paste of pastes) if (context.files.includes(paste.file)) landed.add(paste.file);
    const kept = mapped(context).filter((paste) => !isGone(paste.file, context));
    const changed =
      kept.length !== pastes.length || kept.some((paste, index) => paste !== pastes[index]);
    for (const paste of kept) remember(paste.inputId, context.valueOf(paste.inputId));
    if (!changed) return;
    pastes = kept;
    undoable = false;
    publish();
  };

  const insertAsText = (file: File, context: PastedTextContext) => {
    // Offsets are brought up to now once, before the restore edits the draft.
    const current = mapped(context);
    const paste = current.find((each) => each.file === file);
    if (!paste || isGone(file, context) || context.locked) return false;
    // One restore waits for its input at a time; another runs once it has finished.
    if (pendingRestore !== null && pendingRestore !== file) return false;
    // A reply quote removed since the paste sends the text to the end of the tail.
    const inPlace = context.valueOf(paste.inputId);
    const inputId = inPlace === undefined ? environment.tailInputId : paste.inputId;
    const value = inPlace ?? context.valueOf(environment.tailInputId) ?? "";
    const offset = Math.min(inPlace === undefined ? value.length : paste.offset, value.length);
    const target = context.targetOf(inputId);
    if (!target && inPlace !== undefined && pendingRestore !== file) {
      // The input exists but isn't on screen: show it, then restore through it.
      pendingRestore = file;
      context.revealInput(inputId);
      return true;
    }
    pendingRestore = null;
    if (target) {
      // Typed in through the input as its own undo step, with the caret after it. An input
      // that refuses the edit keeps the attachment.
      target.focus({ preventScroll: true });
      if (!environment.insertText(target, paste.text, offset, offset)) return false;
    } else {
      context.writeInput(inputId, insertAtOffset(value, offset, paste.text));
    }
    // The other pastes move past the restored text, and so do later pastes made at the very
    // same place, which came after this one in the text it replaced.
    const order = pastes.findIndex((each) => each.file === file);
    pastes = current
      .filter((each) => each.file !== file)
      .map((each) => {
        if (each.inputId !== inputId) return each;
        const later = pastes.findIndex((other) => other.file === each.file) > order;
        const moves = each.offset > offset || (each.offset === offset && later);
        return moves ? { ...each, offset: each.offset + paste.text.length } : each;
      });
    // Every offset now counts each input's edits so far, so each input's text is remembered.
    for (const each of pastes) {
      if (each.inputId !== inputId) remember(each.inputId, context.valueOf(each.inputId));
    }
    remember(inputId, insertAtOffset(value, offset, paste.text));
    undoable = false;
    context.removeFile(file);
    publish();
    return true;
  };

  return {
    snapshot: () => snapshot,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    offerPaste: (target, data, context) => {
      const text = pastedPlainText(data);
      if (!exceedsPasteThreshold(text)) return false;
      sequence += 1;
      const file = pastedTextFile(text, sequence);
      // Nothing is consumed unless the attachment is accepted; the owner shows why if not.
      if (!context.attach(file)) return true;
      const inputId = environment.inputIdOf(target) ?? environment.tailInputId;
      const { selectionStart, selectionEnd } = target;
      // Earlier pastes are brought up to now, then carried through the replaced selection.
      pastes = mapped(context);
      for (const paste of pastes) remember(paste.inputId, context.valueOf(paste.inputId));
      if (selectionStart !== selectionEnd) {
        const before = target.value;
        // A locked input refuses the deletion: take the attachment back and leave it as it was.
        if (!environment.insertText(target, "", selectionStart, selectionEnd)) {
          context.removeFile(file);
          return true;
        }
        const after = target.value;
        pastes = pastes.map((paste) =>
          paste.inputId === inputId
            ? { ...paste, offset: mapOffsetThroughEdit(paste.offset, before, after) }
            : paste,
        );
      }
      remember(inputId, target.value);
      pastes = [...pastes, { file, text, inputId, offset: selectionStart }];
      undoable = true;
      publish();
      return true;
    },
    insertAsText,
    undoLatest: (context) => {
      const newest = pastes.at(-1);
      return undoable && newest !== undefined && insertAsText(newest.file, context);
    },
    endUndo: () => {
      if (!undoable) return;
      undoable = false;
      publish();
    },
    sync,
    completePendingRestore: (context) => {
      const file = pendingRestore;
      if (file === null) return;
      // Locked meanwhile: drop the request; the chip stays for a later restore.
      if (context.locked) {
        pendingRestore = null;
        return;
      }
      // Through the input once it shows; a second miss writes the draft directly.
      insertAsText(file, context);
    },
    reset: () => {
      // Every paste restored still leaves its draft's text remembered; a new draft drops it.
      seen.clear();
      pendingRestore = null;
      if (pastes.length === 0 && !undoable) return;
      pastes = [];
      undoable = false;
      publish();
    },
    holdsDraftText: () => seen.size > 0,
  };
}
