// Initials avatars for PR authors, colored so each person in a list stays recognizable.

import { cn } from "@/lib/utils";

/**
 * Space colors evenly and group shared suffixes without provider-specific rules.
 * The same author set keeps its colors; adding or removing authors can shift them.
 */
export function pullRequestAuthorHues(
  authors: readonly (string | null | undefined)[],
): Map<string, number> {
  const identities = authors.flatMap((author) => (author ? [author.toLowerCase()] : []));
  const unique = [...new Set(identities)].sort((a, b) => {
    const reversedA = [...a].reverse().join("");
    const reversedB = [...b].reverse().join("");
    return reversedA < reversedB ? -1 : reversedA > reversedB ? 1 : 0;
  });
  return new Map(unique.map((author, index) => [author, (index * 360) / unique.length]));
}

export function PullRequestAuthorAvatar({
  author,
  hue,
}: {
  author: string | null | undefined;
  hue: number | undefined;
}) {
  return (
    <span
      aria-hidden
      className={cn(
        "inline-flex size-4 shrink-0 items-center justify-center rounded-full text-[9px] font-semibold",
        hue === undefined && "bg-muted text-muted-foreground",
      )}
      style={
        // Uniform perceptual lightness keeps white initials legible across the hue wheel.
        hue === undefined
          ? undefined
          : { backgroundColor: `oklch(0.48 0.13 ${hue})`, color: "#fff" }
      }
    >
      {author?.[0]?.toUpperCase() ?? "?"}
    </span>
  );
}
