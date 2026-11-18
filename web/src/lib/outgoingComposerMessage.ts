import type { Comment } from "@/hooks/useComments";
import {
  expandMentionTokens,
  withSessionMentionHint,
  type InlineMention,
} from "@/lib/composerMentions";
import { formatLocalReviewComments } from "@/lib/localReviewComments";
import {
  appendReplyDraft,
  serializeReplyDraft,
  snapshotReplyDraft,
  type ReplyDraft,
  type StoredReplyDraft,
} from "@/lib/replyDraft";

/** What a composer send carries besides its text. */
export interface ComposerSendContext {
  /** Tagged files and sessions whose tokens are still in the draft. */
  mentions: readonly InlineMention[];
  /** Local review comments attached to the draft. */
  reviewComments: Comment[];
}

/**
 * Whether a send carries more than a bare command: a quote, an attachment, a
 * tag or a review comment makes slash-command text an ordinary message.
 */
export function carriesMessageContext(
  draft: ReplyDraft,
  files: readonly File[],
  { mentions, reviewComments }: ComposerSendContext,
): boolean {
  return (
    draft.quotes.length > 0 || files.length > 0 || mentions.length > 0 || reviewComments.length > 0
  );
}

/**
 * The message a composer sends: each tag token becomes its marker where it
 * sits, review comments follow the authored text, and a tagged session adds
 * one tool hint at the end. *plainText* is the text sent when there are no
 * quotes; with quotes, the draft's own text and quote preambles are used.
 */
export function buildOutgoingMessage(
  draft: ReplyDraft,
  plainText: string,
  { mentions, reviewComments }: ComposerSendContext,
): { text: string; replyDraft?: StoredReplyDraft } {
  const hasAuthoredText = !!draft.text.trim() || draft.quotes.some((quote) => quote.before.trim());
  const reviewContext =
    reviewComments.length > 0
      ? formatLocalReviewComments(reviewComments, { includeInstruction: !hasAuthoredText })
      : "";
  const tagsSessions = mentions.some((mention) => mention.marker.startsWith("[Session: "));
  const expand = (text: string) => expandMentionTokens(text, mentions);
  if (draft.quotes.length > 0) {
    // Preserve authored whitespace and quote provenance, including mention markers.
    const outgoing = appendReplyDraft(
      {
        ...draft,
        text: expand(draft.text),
        quotes: draft.quotes.map((quote) => ({ ...quote, before: expand(quote.before) })),
      },
      { quotes: [], text: reviewContext },
    );
    outgoing.text = withSessionMentionHint(outgoing.text, tagsSessions);
    return { text: serializeReplyDraft(outgoing), replyDraft: snapshotReplyDraft(outgoing) };
  }
  return {
    text: withSessionMentionHint(
      [expand(plainText), reviewContext].filter(Boolean).join("\n\n"),
      tagsSessions,
    ),
  };
}
