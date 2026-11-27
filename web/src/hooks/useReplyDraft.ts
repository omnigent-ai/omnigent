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
  removeQuoteFromDocument,
  replaceQuoteTail,
  unweaveReplyDraft,
  weaveReplyDraft,
} from "@/lib/replyDraftDocument";

const EMPTY_DRAFT: ReplyDraft = { quotes: [], text: "" };

/**
 * The reply draft. In single-document mode, for the CodeMirror composer, the state is the
 * editor's whole document and the draft is read from it. `value` and every text write then
 * address its tail, the text after the last card, so code never adds or removes a card.
 */
export function useReplyDraft(initialSingleDocument = false) {
  // A string is single-document mode's document; a draft is the textarea's.
  const [state, setState] = useState<ReplyDraft | string>(initialSingleDocument ? "" : EMPTY_DRAFT);
  const [focusedId, focusText] = useState<string | null>(null);
  const draft = useMemo(
    () => (typeof state === "string" ? unweaveReplyDraft(state) : state),
    [state],
  );
  const replyDocument = typeof state === "string" ? state : null;
  // The document has no quote inputs, so none is ever active in it.
  const activeTextId = replyDocument === null ? focusedId : null;
  const value = draft.quotes.find((quote) => quote.id === activeTextId)?.before ?? draft.text;

  const editText = useCallback((id: string | null, next: SetStateAction<string>) => {
    setState((current) => {
      const update = (text: string) => (typeof next === "function" ? next(text) : next);
      if (typeof current === "string") {
        if (id !== null) {
          if (import.meta.env.DEV)
            console.warn(`Ignored an edit to quote ${id}'s input in one document.`);
          return current;
        }
        return replaceQuoteTail(current, update(unweaveReplyDraft(current).text));
      }
      return id === null
        ? { ...current, text: update(current.text) }
        : {
            ...current,
            quotes: current.quotes.map((quote) =>
              quote.id === id ? { ...quote, before: update(quote.before) } : quote,
            ),
          };
    });
  }, []);
  const setValue = useCallback(
    (next: SetStateAction<string>) => editText(activeTextId, next),
    [activeTextId, editText],
  );
  // Stored verbatim (the editor already settled it); ignored outside single-document mode.
  const setReplyDocument = useCallback((document: string) => {
    setState((current) => (typeof current === "string" ? document : current));
  }, []);
  const setSingleDocument = useCallback((singleDocument: boolean) => {
    setState((current) => {
      if ((typeof current === "string") === singleDocument) return current;
      return typeof current === "string" ? unweaveReplyDraft(current) : weaveReplyDraft(current);
    });
    focusText(null);
  }, []);
  const replaceText = useCallback((text: string, saved?: StoredReplyDraft) => {
    const restored = restoreReplyDraft(text, saved);
    setState((current) => (typeof current === "string" ? weaveReplyDraft(restored) : restored));
    focusText(null);
  }, []);
  const appendQuote = useCallback((text: string) => {
    setState((current) =>
      typeof current === "string"
        ? appendQuoteToDocument(current, text)
        : appendReplyQuote(current, text),
    );
    focusText(null);
  }, []);
  const removeQuote = useCallback((id: string) => {
    setState((current) =>
      typeof current === "string"
        ? removeQuoteFromDocument(current, id)
        : removeReplyQuote(current, id),
    );
    focusText(null);
  }, []);
  const storedReplyDraft = useMemo(() => snapshotReplyDraft(draft), [draft]);

  return {
    draft,
    value,
    setValue,
    fullText: serializeReplyDraft(draft),
    storedReplyDraft,
    /** The whole document in single-document mode, else null. */
    replyDocument,
    setReplyDocument,
    setSingleDocument,
    activeTextId,
    focusText,
    editText,
    replaceText,
    appendQuote,
    removeQuote,
  };
}
