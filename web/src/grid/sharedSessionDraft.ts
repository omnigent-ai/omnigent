import { useLayoutEffect } from "react";
import { presentTaggedMentions } from "@/lib/composerMentions";
import { restoreReplyDraft, serializeReplyDraft, snapshotReplyDraft } from "@/lib/replyDraft";
import { getSessionDraft, setSessionDraft, type SessionDraft } from "@/lib/sessionDrafts";
import { useChatStore, type SideChatComposerDraft } from "@/store/chatStore";

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

function setComposerEntry(sessionId: string, composer: SideChatComposerDraft | undefined): void {
  useChatStore.setState((state) => {
    const others = Object.fromEntries(
      Object.entries(state.sideChatComposers).filter(([key]) => key !== sessionId),
    );
    return { sideChatComposers: composer ? { ...others, [sessionId]: composer } : others };
  });
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
 * Makes a grid cell's composer and its session's draft one draft, the same one the
 * full-screen composer keeps: the cell starts from it and writes its text, quotes
 * and attachments back as they change. The cell edits the composer entry that a
 * sub-agent tab of the same id also uses, so that entry is put back on unmount.
 */
export function useSharedSessionDraft(sessionId: string): void {
  useLayoutEffect(() => {
    const previous = useChatStore.getState().sideChatComposers[sessionId];
    const draft = getSessionDraft(sessionId);
    const seeded = draft ? composerFromSessionDraft(draft) : undefined;
    setComposerEntry(sessionId, seeded);
    let written = seeded;
    const unsubscribe = useChatStore.subscribe((state) => {
      const composer = state.sideChatComposers[sessionId];
      if (sameComposerContent(composer, written)) return;
      written = composer;
      saveComposerToSessionDraft(sessionId, composer);
    });
    return () => {
      unsubscribe();
      setComposerEntry(sessionId, previous);
    };
  }, [sessionId]);
}
