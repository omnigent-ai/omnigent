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
  typedDocumentFor,
  unweaveReplyDraft,
} from "@/lib/replyDraftDocument";

interface ReplyDraftState {
  draft: ReplyDraft;
  /** The editor's document while it still describes `draft`; see `typedDocumentFor`. */
  typed: string | null;
}

/**
 * The reply draft. With `singleDocument`, the CodeMirror composer shows the whole draft as one
 * document: `value` is that document, and quote operations apply to it.
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
  const value = singleDocument
    ? replyDocument
    : (draft.quotes.find((quote) => quote.id === activeTextId)?.before ?? draft.text);

  const editText = useCallback(
    (id: string | null, next: SetStateAction<string>) => {
      setState((current) => {
        if (singleDocument) {
          const document = typedDocumentFor(current.draft, current.typed);
          const edited = typeof next === "function" ? next(document) : next;
          return { draft: unweaveReplyDraft(edited), typed: edited };
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
          return { draft: unweaveReplyDraft(edited), typed: edited };
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
          const edited = removeReplyQuoteAt(typedDocumentFor(current.draft, current.typed), index);
          return { draft: unweaveReplyDraft(edited), typed: edited };
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
    replaceText,
    appendQuote,
    removeQuote,
  };
}
