import { useCallback, useMemo, useState, type SetStateAction } from "react";
import {
  appendReplyQuote,
  restoreReplyDraft,
  removeReplyQuote,
  serializeReplyDraft,
  snapshotReplyDraft,
  type ReplyDraft,
  type StoredReplyDraft,
} from "@/lib/replyDraft";
import { hasReplyQuoteLines, unweaveReplyDraft, weaveReplyDraft } from "@/lib/replyDraftDocument";

interface DraftState {
  draft: ReplyDraft;
  /**
   * The single editor's document as the user last left it. Kept as typed, since a document can
   * hold blank lines that weaving the draft would not write. Null after a change made in code.
   */
  document: string | null;
}

/**
 * The composer's draft. With `singleDocument` (the CodeMirror engine) the text and its quotes
 * are one document: `value` is that document, and `editText` takes the editor's edits.
 * Otherwise each quote is its own input, as the textarea engine shows them.
 */
export function useReplyDraft({ singleDocument = false }: { singleDocument?: boolean } = {}) {
  const [state, setState] = useState<DraftState>({
    draft: { quotes: [], text: "" },
    document: null,
  });
  const draft = state.draft;
  const [activeTextId, focusText] = useState<string | null>(null);
  const documentValue = useMemo(
    () => state.document ?? weaveReplyDraft(state.draft),
    [state.document, state.draft],
  );
  const value = singleDocument
    ? documentValue
    : (draft.quotes.find((quote) => quote.id === activeTextId)?.before ?? draft.text);

  const updateDraft = useCallback((update: (current: ReplyDraft) => ReplyDraft) => {
    setState((current) => ({ draft: update(current.draft), document: null }));
  }, []);

  const editText = useCallback(
    (id: string | null, next: SetStateAction<string>) => {
      const update = (text: string) => (typeof next === "function" ? next(text) : next);
      if (singleDocument) {
        setState((current) => {
          const document = update(current.document ?? weaveReplyDraft(current.draft));
          return { draft: unweaveReplyDraft(document), document };
        });
        return;
      }
      updateDraft((current) =>
        id === null
          ? { ...current, text: update(current.text) }
          : {
              ...current,
              quotes: current.quotes.map((quote) =>
                quote.id === id ? { ...quote, before: update(quote.before) } : quote,
              ),
            },
      );
    },
    [singleDocument, updateDraft],
  );
  const setValue = useCallback(
    (next: SetStateAction<string>) => {
      if (!singleDocument) {
        editText(activeTextId, next);
        return;
      }
      // Text without quote lines replaces only the trailing text, so a clear keeps the quotes as the textarea does.
      if (typeof next === "string" && !hasReplyQuoteLines(next)) {
        updateDraft((current) => ({ quotes: current.quotes, text: next }));
        return;
      }
      editText(null, next);
    },
    [activeTextId, editText, singleDocument, updateDraft],
  );
  const replaceText = useCallback((text: string, saved?: StoredReplyDraft) => {
    setState({ draft: restoreReplyDraft(text, saved), document: null });
    focusText(null);
  }, []);
  const appendQuote = useCallback(
    (text: string) => {
      updateDraft((current) => appendReplyQuote(current, text));
      focusText(null);
    },
    [updateDraft],
  );
  const removeQuote = useCallback(
    (id: string) => {
      updateDraft((current) => removeReplyQuote(current, id));
      focusText(null);
    },
    [updateDraft],
  );
  const storedReplyDraft = useMemo(() => snapshotReplyDraft(draft), [draft]);

  return {
    draft,
    value,
    setValue,
    fullText: serializeReplyDraft(draft),
    storedReplyDraft,
    // A single document has no quote inputs, so its tail is the one active input.
    activeTextId: singleDocument ? null : activeTextId,
    focusText,
    editText,
    replaceText,
    appendQuote,
    removeQuote,
  };
}
