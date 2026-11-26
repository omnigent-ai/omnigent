// A pull request comment's reactions as this Mac reads them: the eight GitHub offers, how a
// read's fields become pills, how a click not yet confirmed changes them, and who left each.

/** GitHub's reactions, in the order its picker shows them. */
export const REACTION_CONTENTS = [
  "THUMBS_UP",
  "THUMBS_DOWN",
  "LAUGH",
  "HOORAY",
  "CONFUSED",
  "HEART",
  "ROCKET",
  "EYES",
] as const;

export type ReactionContent = (typeof REACTION_CONTENTS)[number];

export const REACTIONS: Record<ReactionContent, { emoji: string; label: string }> = {
  THUMBS_UP: { emoji: "👍", label: "Thumbs Up" },
  THUMBS_DOWN: { emoji: "👎", label: "Thumbs Down" },
  LAUGH: { emoji: "😄", label: "Laugh" },
  HOORAY: { emoji: "🎉", label: "Hooray" },
  CONFUSED: { emoji: "😕", label: "Confused" },
  HEART: { emoji: "❤️", label: "Heart" },
  ROCKET: { emoji: "🚀", label: "Rocket" },
  EYES: { emoji: "👀", label: "Eyes" },
};

/** How many reactors a read names; the rest are only counted. */
export const NAMED_REACTORS = 10;

/** One reaction someone left on a comment, as a read on this Mac shapes it. */
export interface CommentReaction {
  content: ReactionContent;
  count: number;
  viewer_reacted: boolean;
  /** Up to {@link NAMED_REACTORS} of the people who left it. */
  logins: string[];
}

/** A comment's reactions and whether the viewer may react; absent from older reads. */
export interface CommentReactions {
  reactions: CommentReaction[];
  canReact: boolean;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export function isReactionContent(value: unknown): value is ReactionContent {
  return typeof value === "string" && (REACTION_CONTENTS as readonly string[]).includes(value);
}

/** The reactions in `value`, or null unless every one is well formed and none repeats. */
export function parseReactionList(value: unknown): CommentReaction[] | null {
  if (!Array.isArray(value)) return null;
  const seen = new Set<string>();
  const reactions: CommentReaction[] = [];
  for (const item of value) {
    if (!isObject(item) || !isReactionContent(item.content) || seen.has(item.content)) return null;
    const { count, viewer_reacted: viewerReacted, logins } = item;
    if (!Number.isInteger(count) || (count as number) < 1) return null;
    if (typeof viewerReacted !== "boolean") return null;
    // The host's reads count reactions without naming who left them.
    const named = logins ?? [];
    if (!Array.isArray(named) || !named.every((login) => typeof login === "string")) return null;
    seen.add(item.content);
    reactions.push({
      content: item.content,
      count: count as number,
      viewer_reacted: viewerReacted,
      logins: named.slice(0, NAMED_REACTORS),
    });
  }
  return inPickerOrder(reactions);
}

/**
 * A comment's reactions from its read, or null where the read doesn't carry them: the host's,
 * an older shell's, or a malformed one. Null shows no reactions and offers none.
 */
export function commentReactionsOf(comment: {
  reactions?: unknown;
  can_react?: unknown;
}): CommentReactions | null {
  if (typeof comment.can_react !== "boolean") return null;
  const reactions = parseReactionList(comment.reactions);
  return reactions ? { reactions, canReact: comment.can_react } : null;
}

function inPickerOrder(reactions: CommentReaction[]): CommentReaction[] {
  return [...reactions].sort(
    (left, right) =>
      REACTION_CONTENTS.indexOf(left.content) - REACTION_CONTENTS.indexOf(right.content),
  );
}

const sameLogin = (left: string, right: string) => left.toLowerCase() === right.toLowerCase();

/**
 * `reactions` with the viewer's `content` added or removed, as a click not yet confirmed shows
 * it. A reaction already as asked is left alone; one nobody has left any more goes.
 */
export function withViewerReaction(
  reactions: readonly CommentReaction[],
  content: ReactionContent,
  add: boolean,
  viewerLogin: string,
): CommentReaction[] {
  const existing = reactions.find((reaction) => reaction.content === content);
  if ((existing?.viewer_reacted ?? false) === add) return [...reactions];
  if (add) {
    // Where the viewer's login isn't known, as on the host's route, "You" still names them.
    const named = viewerLogin ? [viewerLogin] : [];
    const added: CommentReaction = existing
      ? {
          ...existing,
          count: existing.count + 1,
          viewer_reacted: true,
          logins: [
            ...named,
            ...existing.logins.filter((login) => !sameLogin(login, viewerLogin)),
          ].slice(0, NAMED_REACTORS),
        }
      : { content, count: 1, viewer_reacted: true, logins: named };
    return inPickerOrder([...reactions.filter((reaction) => reaction.content !== content), added]);
  }
  // `existing` is the viewer's, so it's there.
  const left = (existing as CommentReaction).count - 1;
  return reactions.flatMap((reaction) => {
    if (reaction.content !== content) return [reaction];
    if (left < 1) return [];
    return [
      {
        ...reaction,
        count: left,
        viewer_reacted: false,
        logins: reaction.logins.filter((login) => !sameLogin(login, viewerLogin)),
      },
    ];
  });
}

/** "a, b and c": the names in a sentence. */
function listed(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/**
 * Who left `reaction`, for its tooltip: "You, alice and 3 others reacted with Heart". The
 * viewer reads as "You", first; GitHub names at most ten, and the rest are counted.
 */
export function reactorsSummary(reaction: CommentReaction, viewerLogin: string | null): string {
  const viewer = viewerLogin ?? "";
  const others = reaction.logins.filter((login) => !viewer || !sameLogin(login, viewer));
  const names = [
    ...(reaction.viewer_reacted ? ["You"] : []),
    ...others.map((login) => `@${login}`),
    // Without the viewer's login, GitHub's list may name them again; never name more than reacted.
  ].slice(0, Math.min(NAMED_REACTORS, reaction.count));
  const unnamed = Math.max(0, reaction.count - names.length);
  const label = REACTIONS[reaction.content].label;
  if (names.length === 0) {
    return `${reaction.count === 1 ? "1 person" : `${reaction.count} people`} reacted with ${label}`;
  }
  const people =
    unnamed > 0
      ? `${names.join(", ")} and ${unnamed === 1 ? "1 other" : `${unnamed} others`}`
      : listed(names);
  return `${people} reacted with ${label}`;
}

/** What the reaction read says of a comment: its reactions now, gone, or not knowable. */
export type CommentReactionsState =
  | { state: "present"; reactions: CommentReaction[]; canReact: boolean }
  | { state: "absent" }
  | { state: "unreadable" };

/** A reaction write's answer, or the reaction read's: the comment's reactions as GitHub has them. */
export function parseCommentReactionsAnswer(
  data: unknown,
  commentId: string,
): CommentReactionsState | null {
  if (!isObject(data) || data.object !== "session.github.pr_comment_reactions") return null;
  if (data.comment_id !== commentId) return null;
  if (data.state === "absent" || data.state === "unreadable") return { state: data.state };
  if (data.state !== "present" || typeof data.can_react !== "boolean") return null;
  const reactions = parseReactionList(data.reactions);
  return reactions ? { state: "present", reactions, canReact: data.can_react } : null;
}
