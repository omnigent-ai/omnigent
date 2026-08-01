import { useCallback, useRef, type RefObject } from "react";

type SetDraft = (updater: (prev: string) => string) => void;

interface Selection {
  start: number;
  end: number;
}

interface OwnedRegion extends Selection {
  text: string;
  restoreText: string;
}

interface Insertion {
  draft: string;
  owned: OwnedRegion;
  caret: number;
}

function clampSelection(start: number, end: number, length: number): Selection {
  const clampedStart = Math.max(0, Math.min(start, length));
  const clampedEnd = Math.max(clampedStart, Math.min(end, length));
  return { start: clampedStart, end: clampedEnd };
}

function textareaSelection(
  textarea: HTMLTextAreaElement | null,
  draft: string,
  replaceSelection: boolean,
): Selection {
  if (!textarea) return { start: draft.length, end: draft.length };
  const selection = clampSelection(textarea.selectionStart, textarea.selectionEnd, draft.length);
  return replaceSelection ? selection : { start: selection.end, end: selection.end };
}

function insertAt(
  draft: string,
  selection: Selection,
  text: string,
  restoreText = draft.slice(selection.start, selection.end),
): Insertion {
  const before = draft.slice(0, selection.start);
  const after = draft.slice(selection.end);
  const prefix = text && before && !/\s$/.test(before) && !/^\s/.test(text) ? " " : "";
  const suffix = text && after && !/\s$/.test(text) && !/^\s/.test(after) ? " " : "";
  const inserted = prefix + text + suffix;

  return {
    draft: before + inserted + after,
    owned: {
      start: selection.start,
      end: selection.start + inserted.length,
      text: inserted,
      restoreText,
    },
    caret: selection.start + prefix.length + text.length,
  };
}

function ownsRegion(draft: string, region: OwnedRegion | null): region is OwnedRegion {
  return (
    region !== null &&
    region.end <= draft.length &&
    draft.slice(region.start, region.end) === region.text
  );
}

export function useDictationInsert(
  draft: string,
  setDraft: SetDraft,
  textareaRef: RefObject<HTMLTextAreaElement | null>,
): {
  /** Capture the current selection immediately before a dictation take starts. */
  begin: () => Selection;
  /** Append a final utterance, replacing any pending interim region. */
  appendFinal: (text: string) => void;
  /** Replace the pending interim region ("" clears it). */
  replaceInterim: (text: string) => void;
  /** Rebase an owned interim around a user edit, or invalidate it on overlap. */
  reconcileUserEdit: (next: string) => void;
  /** Drop selection and ownership markers after an external draft replacement. */
  reset: () => void;
} {
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const initialSelectionRef = useRef<Selection | null>(null);
  const ownedRef = useRef<OwnedRegion | null>(null);
  const caretRef = useRef<number | null>(null);

  const select = useCallback(
    (caret: number) => {
      queueMicrotask(() => textareaRef.current?.setSelectionRange(caret, caret));
    },
    [textareaRef],
  );

  const reset = useCallback(() => {
    initialSelectionRef.current = null;
    ownedRef.current = null;
    caretRef.current = null;
  }, []);

  const begin = useCallback(() => {
    const selection = textareaSelection(textareaRef.current, draftRef.current, true);
    initialSelectionRef.current = selection;
    ownedRef.current = null;
    caretRef.current = selection.end;
    return selection;
  }, [textareaRef]);

  const reconcileUserEdit = useCallback((next: string) => {
    const previous = draftRef.current;
    const owned = ownedRef.current;
    draftRef.current = next;
    let prefix = 0;
    const shared = Math.min(previous.length, next.length);
    while (prefix < shared && previous[prefix] === next[prefix]) prefix += 1;

    let suffix = 0;
    while (
      suffix < shared - prefix &&
      previous[previous.length - 1 - suffix] === next[next.length - 1 - suffix]
    ) {
      suffix += 1;
    }
    const oldEditEnd = previous.length - suffix;

    if (!ownsRegion(previous, owned)) {
      ownedRef.current = null;
      const initial = initialSelectionRef.current;
      if (initial) {
        if (oldEditEnd <= initial.start) {
          const delta = next.length - previous.length;
          initialSelectionRef.current = {
            start: initial.start + delta,
            end: initial.end + delta,
          };
          caretRef.current = initial.end + delta;
        } else if (prefix < initial.end) {
          initialSelectionRef.current = null;
          caretRef.current = null;
        }
      } else {
        caretRef.current = null;
      }
      return;
    }
    const caret = caretRef.current;
    const insertedAtCaret =
      caret !== null && prefix === caret && oldEditEnd === caret && next.length > previous.length;
    if (insertedAtCaret) {
      ownedRef.current = {
        ...owned,
        end: caret,
        text: previous.slice(owned.start, caret),
      };
      caretRef.current = caret + (next.length - previous.length);
    } else if (oldEditEnd <= owned.start) {
      const delta = next.length - previous.length;
      ownedRef.current = { ...owned, start: owned.start + delta, end: owned.end + delta };
      if (caretRef.current !== null) caretRef.current += delta;
    } else if (prefix < owned.end) {
      ownedRef.current = null;
      caretRef.current = null;
    }
    initialSelectionRef.current = null;
  }, []);

  const insertionSelection = useCallback(
    (current: string): Selection => {
      const owned = ownedRef.current;
      if (ownsRegion(current, owned)) return { start: owned.start, end: owned.end };
      if (owned) {
        ownedRef.current = null;
        caretRef.current = null;
      }
      if (initialSelectionRef.current) {
        return clampSelection(
          initialSelectionRef.current.start,
          initialSelectionRef.current.end,
          current.length,
        );
      }
      if (caretRef.current !== null) {
        const caret = Math.max(0, Math.min(caretRef.current, current.length));
        return { start: caret, end: caret };
      }
      return textareaSelection(textareaRef.current, current, false);
    },
    [textareaRef],
  );

  const replaceInterim = useCallback(
    (text: string) => {
      const current = draftRef.current;
      const owned = ownedRef.current;

      if (!text) {
        if (ownsRegion(current, owned)) {
          const next = current.slice(0, owned.start) + owned.restoreText + current.slice(owned.end);
          draftRef.current = next;
          setDraft(() => next);
          caretRef.current = owned.start + owned.restoreText.length;
          select(owned.start + owned.restoreText.length);
        }
        initialSelectionRef.current = null;
        ownedRef.current = null;
        return;
      }

      const ownedRestoreText = ownsRegion(current, owned) ? owned.restoreText : undefined;
      const insertion = insertAt(current, insertionSelection(current), text, ownedRestoreText);
      draftRef.current = insertion.draft;
      setDraft(() => insertion.draft);
      initialSelectionRef.current = null;
      ownedRef.current = insertion.owned;
      caretRef.current = insertion.caret;
      select(insertion.caret);
    },
    [insertionSelection, select, setDraft],
  );

  const appendFinal = useCallback(
    (text: string) => {
      if (!text) return;
      const current = draftRef.current;
      const insertion = insertAt(current, insertionSelection(current), text);
      draftRef.current = insertion.draft;
      setDraft(() => insertion.draft);
      initialSelectionRef.current = null;
      ownedRef.current = null;
      caretRef.current = insertion.caret;
      select(insertion.caret);
    },
    [insertionSelection, select, setDraft],
  );

  return { begin, appendFinal, replaceInterim, reconcileUserEdit, reset };
}
