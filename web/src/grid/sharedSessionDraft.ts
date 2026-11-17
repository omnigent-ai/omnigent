import { useLayoutEffect } from "react";
import { presentTaggedMentions } from "@/lib/composerMentions";
import { restoreReplyDraft, serializeReplyDraft, snapshotReplyDraft } from "@/lib/replyDraft";
import {
  getSessionDraft,
  setSessionDraft,
  subscribeSessionDrafts,
  type SessionDraft,
} from "@/lib/sessionDrafts";
import { gridCellComposerKey, useChatStore, type SideChatComposerDraft } from "@/store/chatStore";

/** The composer entry a session draft seeds: its text, quote cards and attachments. */
function composerFromSessionDraft(draft: SessionDraft): SideChatComposerDraft {
  const { text, quotes } = restoreReplyDraft(draft.text, draft.replyDraft);
  return { text, files: draft.files, ...(quotes.length > 0 ? { quotes } : {}) };
}

/** Write a composer entry into its session's draft; tags whose token is gone drop out. */
function saveComposerToSessionDraft(
  sessionId: string,
  composer: SideChatComposerDraft | undefined,
): void {
  const text = composer?.text ?? "";
  const quotes = composer?.quotes ?? [];
  const mentions = presentTaggedMentions(getSessionDraft(sessionId)?.mentions, [
    text,
    ...quotes.map((quote) => quote.before),
  ]);
  setSessionDraft(sessionId, {
    text: serializeReplyDraft({ quotes, text }),
    files: composer?.files ?? [],
    replyDraft: snapshotReplyDraft({ quotes, text }),
    ...(mentions ? { mentions } : {}),
  });
}

function setComposerEntry(key: string, composer: SideChatComposerDraft | undefined): void {
  useChatStore.setState((state) => {
    const others = Object.fromEntries(
      Object.entries(state.sideChatComposers).filter(([id]) => id !== key),
    );
    return { sideChatComposers: composer ? { ...others, [key]: composer } : others };
  });
}

/** Whether the composer already shows the session draft's text and attachments. */
function sameDraftContent(
  draft: SessionDraft | undefined,
  composer: SideChatComposerDraft | undefined,
): boolean {
  const files = composer?.files ?? [];
  const draftFiles = draft?.files ?? [];
  const text = serializeReplyDraft({ quotes: composer?.quotes ?? [], text: composer?.text ?? "" });
  return (
    (draft?.text ?? "") === text &&
    files.length === draftFiles.length &&
    files.every((file, index) => file === draftFiles[index])
  );
}

function sameComposerContent(
  first: SideChatComposerDraft | undefined,
  second: SideChatComposerDraft | undefined,
): boolean {
  return (
    first?.text === second?.text &&
    first?.quotes === second?.quotes &&
    first?.files === second?.files
  );
}

/**
 * Keeps a grid cell's composer and its session's draft one draft, the same one the
 * full-screen composer keeps. The cell edits its own composer entry: it starts from
 * the session draft, writes edits back, and re-reads the draft when another writer
 * changes it.
 */
export function useSharedSessionDraft(sessionId: string): void {
  useLayoutEffect(() => {
    const key = gridCellComposerKey(sessionId);
    let written: SideChatComposerDraft | undefined;
    const reseed = () => {
      const draft = getSessionDraft(sessionId);
      if (sameDraftContent(draft, useChatStore.getState().sideChatComposers[key])) return;
      written = draft ? composerFromSessionDraft(draft) : undefined;
      setComposerEntry(key, written);
    };
    reseed();
    const unsubscribeDrafts = subscribeSessionDrafts(reseed);
    const unsubscribeComposer = useChatStore.subscribe((state) => {
      const composer = state.sideChatComposers[key];
      if (sameComposerContent(composer, written)) return;
      written = composer;
      saveComposerToSessionDraft(sessionId, composer);
    });
    return () => {
      unsubscribeDrafts();
      unsubscribeComposer();
      setComposerEntry(key, undefined);
    };
  }, [sessionId]);
}
