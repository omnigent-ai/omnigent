// A draft's @-mention tags, active and released. A tag whose token leaves the draft is released
// rather than dropped: it keeps its token, so no other target takes it, and an undo or redo
// that brings the token back can restore the tag. A new draft forgets released tags.

import {
  fileMentionToken,
  findMentionTokens,
  newFileMentionToken,
  type MentionItem,
  type SessionMention,
  sessionMentionToken,
  type TaggedMentions,
} from "@/lib/composerMentions";
import { composerAttachmentKey } from "@/store/chatStore";

export const NO_TAGS: TaggedMentions = { files: [], sessions: [] };

/** The token a tagged session stands as in the draft. */
export function sessionTagToken(session: SessionMention): string {
  return session.token ?? `@${session.title}`;
}

/** Every token `tags` own, files first. */
export function tagTokens(tags: TaggedMentions): string[] {
  return [...tags.files.map(fileMentionToken), ...tags.sessions.map(sessionTagToken)];
}

/** `tags` without anything `removed` names by token. */
export function withoutTags(tags: TaggedMentions, removed: TaggedMentions): TaggedMentions {
  const gone = new Set(tagTokens(removed));
  return {
    files: tags.files.filter((file) => !gone.has(fileMentionToken(file))),
    sessions: tags.sessions.filter((session) => !gone.has(sessionTagToken(session))),
  };
}

/** `tags` plus `added`, which replace any with the same tokens. */
export function mergeTags(tags: TaggedMentions, added: TaggedMentions): TaggedMentions {
  const kept = withoutTags(tags, added);
  return {
    files: [...kept.files, ...added.files],
    sessions: [...kept.sessions, ...added.sessions],
  };
}

/** How many times each of `tokens` stands in `text`, scanning them together. */
function tokenCounts(text: string, tokens: readonly string[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const range of findMentionTokens(text, tokens)) {
    counts.set(range.token, (counts.get(range.token) ?? 0) + 1);
  }
  return counts;
}

/**
 * The tags in `active` whose tokens stand in none of `segments`, the draft's editable texts.
 * Every owned token is scanned at once, so one that is only the start of a longer token doesn't
 * count as present.
 */
export function tagsGoneFrom(
  active: TaggedMentions,
  segments: readonly string[],
  owned: readonly string[],
): TaggedMentions {
  const present = new Set(
    segments.flatMap((segment) => findMentionTokens(segment, owned).map((range) => range.token)),
  );
  return {
    files: active.files.filter((file) => !present.has(fileMentionToken(file))),
    sessions: active.sessions.filter((session) => !present.has(sessionTagToken(session))),
  };
}

/**
 * The released tags an undo or redo brought back: those whose token stands more often in the
 * input's text after the edit than before. A token that was already there stays literal.
 *
 * @param released The draft's released tags.
 * @param before The input's text before the undo or redo.
 * @param after Its text after.
 * @param owned Every token a tag owns, active or released.
 */
export function tagsRestoredByHistory(
  released: TaggedMentions,
  before: string,
  after: string,
  owned: readonly string[],
): TaggedMentions {
  const countBefore = tokenCounts(before, owned);
  const restored = new Set(
    [...tokenCounts(after, owned)]
      .filter(([token, count]) => count > (countBefore.get(token) ?? 0))
      .map(([token]) => token),
  );
  return {
    files: released.files.filter((file) => restored.has(fileMentionToken(file))),
    sessions: released.sessions.filter((session) => restored.has(sessionTagToken(session))),
  };
}

/** Where a tag came from: already tagged, taken back from the released tags, or new. */
export type TagSource = "active" | "released" | "new";

/**
 * The tag for `file`, reusing an active or released one for the same attachment (path, kind and
 * line range), so the `@` menu and "Attach to agent" never disagree about duplicates. A new tag
 * takes a token no tag owns.
 *
 * @param file The file to tag.
 * @param tags The draft's active and released tags.
 * @param owned Every token a tag owns, active or released.
 */
export function claimFileTag(
  file: MentionItem,
  tags: { active: TaggedMentions; released: TaggedMentions },
  owned: readonly string[],
): { item: MentionItem; source: TagSource } {
  const key = composerAttachmentKey(file);
  const active = tags.active.files.find((each) => composerAttachmentKey(each) === key);
  if (active) return { item: active, source: "active" };
  const released = tags.released.files.find((each) => composerAttachmentKey(each) === key);
  if (released) return { item: released, source: "released" };
  return { item: { ...file, token: newFileMentionToken(file, owned) }, source: "new" };
}

/**
 * The tag for `session`: its active one, else its released one, else a new one whose token no
 * tag owns. A released tag keeps the token the draft's text still names.
 */
export function claimSessionTag(
  session: SessionMention,
  tags: { active: TaggedMentions; released: TaggedMentions },
  owned: readonly string[],
): { session: SessionMention; source: TagSource } {
  const active = tags.active.sessions.find((each) => each.id === session.id);
  if (active) return { session: active, source: "active" };
  const released = tags.released.sessions.find((each) => each.id === session.id);
  if (released) return { session: released, source: "released" };
  return { session: { ...session, token: sessionMentionToken(session, owned) }, source: "new" };
}
