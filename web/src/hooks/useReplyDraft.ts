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
import {
  appendQuoteToDocument,
  hasReplyQuoteLines,
  removeReplyQuoteLine,
  replaceTrailingText,
  replyQuoteLineIndex,
  typedDocumentFor,
  unweaveReplyDraft,
} from "@/lib/replyDraftDocument";

interface DraftState {
  draft: ReplyDraft;
  /**
   * The single editor's document as the user left it. It keeps what the user typed, such as blank
   * lines weaving would not write. Null when only code has changed the draft.
   */
  document: string | null;
}

/**
 * The composer's draft. With `singleDocument` (the CodeMirror engine) the text and its quotes
 * are one document: `value` is that document, and `editText` takes the editor's edits. Changes
 * from code edit the document too, so what the user typed stays as it was.
 * Otherwise each quote is its own input, as the textarea engine shows them.
 */
export function useReplyDraft({ singleDocument = false }: { singleDocument?: boolean } = {}) {
  const [state, setState] = useState<DraftState>({
    draft: { quotes: [], text: "" },
    document: null,
  });
  const draft = state.draft;
  const [activeTextId, focusText] = useState<string | null>(null);
  const documentValue = typedDocumentFor(draft, state.document);
  const value = singleDocument
    ? documentValue
    : (draft.quotes.find((quote) => quote.id === activeTextId)?.before ?? draft.text);

  /** Applies `update`; in single-document mode, `updateDocument` applies the same change to the document. */
  const updateDraft = useCallback(
    (
      update: (current: ReplyDraft) => ReplyDraft,
      updateDocument?: (document: string, next: ReplyDraft) => string,
    ) => {
      setState((current) => {
        const next = update(current.draft);
        if (!singleDocument || !updateDocument) return { draft: next, document: null };
        const document = updateDocument(typedDocumentFor(current.draft, current.document), next);
        return { draft: unweaveReplyDraft(document), document };
      });
    },
    [singleDocument],
  );

  const editText = useCallback(
    (id: string | null, next: SetStateAction<string>) => {
      const update = (text: string) => (typeof next === "function" ? next(text) : next);
      if (singleDocument) {
        setState((current) => {
          const document = update(typedDocumentFor(current.draft, current.document));
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
        updateDraft(
          (current) => ({ quotes: current.quotes, text: next }),
          (document) => replaceTrailingText(document, next),
        );
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
      updateDraft(
        (current) => appendReplyQuote(current, text),
        (document, next) => appendQuoteToDocument(document, next.quotes[next.quotes.length - 1]!),
      );
      focusText(null);
    },
    [updateDraft],
  );
  const removeQuote = useCallback(
    (id: string) => {
      updateDraft(
        (current) => removeReplyQuote(current, id),
        (document) => removeReplyQuoteLine(document, replyQuoteLineIndex(document, id)),
      );
      focusText(null);
    },
    [updateDraft],
  );
  const storedReplyDraft = useMemo(() => snapshotReplyDraft(draft), [draft]);

  return {
    draft,
    value,
    /** The whole document, whichever engine shows it: the caret maps between engines through it. */
    document: documentValue,
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
