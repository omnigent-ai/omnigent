import { useCallback, useMemo, useState, type SetStateAction } from "react";
import { nanoid } from "nanoid";
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
  removeReplyQuoteAt,
  replaceTail,
  tailOf,
  typedDocumentFor,
  unweaveReplyDraft,
} from "@/lib/replyDraftDocument";

interface ReplyDraftState {
  draft: ReplyDraft;
  /** The editor's document while it still describes `draft`; see `typedDocumentFor`. */
  typed: string | null;
}

/** The state for a document the editor holds. Its own rules have already applied to it. */
function stateOfDocument(document: string): ReplyDraftState {
  return { draft: unweaveReplyDraft(document), typed: document };
}

/**
 * The reply draft. With `singleDocument`, the CodeMirror composer shows the whole draft as one
 * document. `value` is its tail, the text after the last card, and every text write addresses that
 * tail. A write never removes or moves a card.
 */
export function useReplyDraft(singleDocument = false) {
  const [{ draft, typed }, setState] = useState<ReplyDraftState>({
    draft: { quotes: [], text: "" },
    typed: null,
  });
  const [focusedId, focusText] = useState<string | null>(null);
  const replyDocument = useMemo(() => typedDocumentFor(draft, typed), [draft, typed]);
  // Single-document mode has no quote inputs, so no quote is ever the active one.
  const activeTextId = singleDocument ? null : focusedId;
  // `value` is the text the writes below address: the tail in one document, else the active input.
  // The whole document is `replyDocument`.
  const value = singleDocument
    ? tailOf(replyDocument)
    : (draft.quotes.find((quote) => quote.id === activeTextId)?.before ?? draft.text);

  const editText = useCallback(
    (id: string | null, next: SetStateAction<string>) => {
      setState((current) => {
        if (singleDocument) {
          // A quote's own input is gone in this mode, so an edit addressed to one is dropped.
          if (id !== null) return current;
          const document = typedDocumentFor(current.draft, current.typed);
          const tail = typeof next === "function" ? next(tailOf(document)) : next;
          return stateOfDocument(replaceTail(document, tail));
        }
        const update = (text: string) => (typeof next === "function" ? next(text) : next);
        const draftNext =
          id === null
            ? { ...current.draft, text: update(current.draft.text) }
            : {
                ...current.draft,
                quotes: current.draft.quotes.map((quote) =>
                  quote.id === id ? { ...quote, before: update(quote.before) } : quote,
                ),
              };
        return { draft: draftNext, typed: null };
      });
    },
    [singleDocument],
  );
  const setValue = useCallback(
    (next: SetStateAction<string>) => editText(activeTextId, next),
    [activeTextId, editText],
  );
  // The editor's own edit, as it stands: its rules have already applied to it.
  const setEditorDocument = useCallback((document: string) => {
    setState(stateOfDocument(document));
  }, []);
  const replaceText = useCallback((text: string, saved?: StoredReplyDraft) => {
    setState({ draft: restoreReplyDraft(text, saved), typed: null });
    focusText(null);
  }, []);
  const appendQuote = useCallback(
    (text: string) => {
      setState((current) => {
        if (singleDocument) {
          const edited = appendQuoteToDocument(typedDocumentFor(current.draft, current.typed), {
            id: nanoid(),
            text: text.replace(/\r\n?/g, "\n"),
          });
          return stateOfDocument(edited);
        }
        return { draft: appendReplyQuote(current.draft, text), typed: null };
      });
      focusText(null);
    },
    [singleDocument],
  );
  const removeQuote = useCallback(
    (id: string) => {
      setState((current) => {
        if (singleDocument) {
          const index = current.draft.quotes.findIndex((quote) => quote.id === id);
          if (index < 0) return current;
          const document = typedDocumentFor(current.draft, current.typed);
          return stateOfDocument(removeReplyQuoteAt(document, index));
        }
        return { draft: removeReplyQuote(current.draft, id), typed: null };
      });
      focusText(null);
    },
    [singleDocument],
  );
  const storedReplyDraft = useMemo(() => snapshotReplyDraft(draft), [draft]);

  return {
    draft,
    value,
    setValue,
    fullText: serializeReplyDraft(draft),
    storedReplyDraft,
    replyDocument,
    activeTextId,
    focusText,
    editText,
    setEditorDocument,
    replaceText,
    appendQuote,
    removeQuote,
  };
}
