import { useCallback, useMemo, useState, type SetStateAction } from "react";
import { nanoid } from "nanoid";
import {
  restoreReplyDraft,
  removeReplyQuote,
  serializeReplyDraft,
  snapshotReplyDraft,
  type ReplyDraft,
  type StoredReplyDraft,
} from "@/lib/replyDraft";

export function useReplyDraft() {
  const [draft, setDraft] = useState<ReplyDraft>({ quotes: [], text: "" });
  const [activeTextId, focusText] = useState<string | null>(null);
  // Whether this quoted draft opens a side chat rather than replying inline.
  // In-memory only: a draft restored from storage degrades to a normal reply
  // (the quote card stays; the send just goes to the main chat).
  const [sideChat, setSideChat] = useState(false);
  const value = draft.quotes.find((quote) => quote.id === activeTextId)?.before ?? draft.text;

  const editText = useCallback((id: string | null, next: SetStateAction<string>) => {
    setDraft((current) => {
      const update = (text: string) => (typeof next === "function" ? next(text) : next);
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
  const replaceText = useCallback((text: string, saved?: StoredReplyDraft) => {
    setDraft(restoreReplyDraft(text, saved));
    focusText(null);
    setSideChat(false);
  }, []);
  const appendQuote = useCallback((text: string) => {
    const id = nanoid();
    setDraft((current) => ({
      quotes: [...current.quotes, { id, before: current.text, text: text.replace(/\r\n?/g, "\n") }],
      text: "",
    }));
    focusText(null);
  }, []);
  // Append a quote and mark the draft as opening a side chat. The card renders
  // exactly like a reply quote; on send the composer routes it to /side.
  const beginSideChatQuote = useCallback(
    (text: string) => {
      appendQuote(text);
      setSideChat(true);
    },
    [appendQuote],
  );
  const removeQuote = useCallback((id: string) => {
    setDraft((current) => {
      const next = removeReplyQuote(current, id);
      // Dropping the last quote makes this an ordinary message again.
      if (next.quotes.length === 0) setSideChat(false);
      return next;
    });
    focusText(null);
  }, []);
  const storedReplyDraft = useMemo(() => snapshotReplyDraft(draft), [draft]);

  return {
    draft,
    value,
    setValue,
    fullText: serializeReplyDraft(draft),
    storedReplyDraft,
    activeTextId,
    focusText,
    editText,
    replaceText,
    appendQuote,
    beginSideChatQuote,
    sideChat,
    removeQuote,
  };
}
