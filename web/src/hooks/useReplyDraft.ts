import { useCallback, useState, type SetStateAction } from "react";
import { nanoid } from "nanoid";
import {
  parseReplyDraft,
  removeReplyQuote,
  serializeReplyDraft,
  type ReplyDraft,
} from "@/lib/replyDraft";

export function useReplyDraft() {
  const [draft, setDraft] = useState<ReplyDraft>({ quotes: [], text: "" });
  const [activeTextId, focusText] = useState<string | null>(null);
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
  const replaceText = useCallback((text: string) => {
    setDraft(parseReplyDraft(text));
    focusText(null);
  }, []);
  const appendQuote = useCallback((text: string) => {
    const id = nanoid();
    setDraft((current) => ({
      quotes: [...current.quotes, { id, before: current.text, text: text.replace(/\r\n?/g, "\n") }],
      text: "",
    }));
    focusText(null);
  }, []);
  const removeQuote = useCallback((id: string) => {
    setDraft((current) => removeReplyQuote(current, id));
    focusText(null);
  }, []);

  return {
    draft,
    value,
    setValue,
    fullText: serializeReplyDraft(draft),
    activeTextId,
    focusText,
    editText,
    replaceText,
    appendQuote,
    removeQuote,
  };
}
