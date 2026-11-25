// A comment's reactions under its body: one pill per reaction with its count, pressed where the
// viewer left it, naming who did on hover; and the picker of GitHub's eight. One tab stop for
// the row, arrows between its pills, as for the picker's reactions.

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Spinner } from "@/components/ui/spinner";
import { Toolbar, ToolbarButton } from "@/components/ui/toolbar";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  REACTION_CONTENTS,
  REACTIONS,
  type ReactionContent,
  reactorsSummary,
} from "@/lib/pullRequestCommentReactions";
import { cn } from "@/lib/utils";

import type { CommentReactionsCard } from "./PullRequestCommentActions";

const PILL =
  "inline-flex h-6 shrink-0 items-center gap-1 rounded-full border border-border px-2 text-xs tabular-nums text-muted-foreground outline-none transition-colors focus-visible:ring-3 focus-visible:ring-ring/50 not-aria-disabled:hover:bg-accent not-aria-disabled:hover:text-accent-foreground aria-disabled:cursor-default aria-pressed:border-primary/50 aria-pressed:bg-primary/10 aria-pressed:text-foreground data-pending:opacity-70";

/** The reaction row, or nothing where there are none to show and none to add. */
export function CommentReactionsBar({
  reactions: card,
  className,
}: {
  reactions: CommentReactionsCard;
  className?: string;
}) {
  const { model, pickerOpen, setPickerOpen, commands } = card;
  if (!model) return null;
  const { addReaction } = commands;
  const { shown, canReact } = model;
  if (shown.reactions.length === 0 && !canReact) return null;
  return (
    <Toolbar
      aria-label="Reactions"
      aria-busy={shown.rereading || undefined}
      data-comment-reactions=""
      className={cn("mt-2 flex flex-wrap items-center gap-1", className)}
    >
      {shown.reactions.map((reaction) => {
        const { emoji, label } = REACTIONS[reaction.content];
        const summary = reactorsSummary(reaction, model.viewerLogin);
        return (
          <Tooltip key={reaction.content}>
            <TooltipTrigger asChild>
              <ToolbarButton
                type="button"
                className={PILL}
                aria-pressed={reaction.viewer_reacted}
                aria-label={`${label}, ${reaction.count}. ${summary}`}
                data-reaction={reaction.content}
                data-pending={shown.pending.has(reaction.content) ? "" : undefined}
                // Read-only, but still focusable, so its tooltip says who reacted.
                aria-disabled={!canReact || undefined}
                onClick={() => model.toggle(reaction.content)}
              >
                <span aria-hidden>{emoji}</span>
                <span aria-hidden>{reaction.count}</span>
              </ToolbarButton>
            </TooltipTrigger>
            <TooltipContent>{summary}</TooltipContent>
          </Tooltip>
        );
      })}
      {addReaction && (
        // The smiley runs the card's Add Reaction… command, as its ⋯ menu and ⌘K do. It stays
        // the popover's trigger, so closing the picker returns focus to it from any entry point.
        <Popover open={pickerOpen} onOpenChange={(open) => !open && setPickerOpen(false)}>
          <PopoverTrigger asChild>
            <ToolbarButton
              type="button"
              aria-label={addReaction.definition.menuLabel}
              title={addReaction.definition.menuLabel}
              data-add-reaction=""
              onClick={addReaction.run}
              className={cn(
                PILL,
                "px-1.5",
                // Quiet until the comment is hovered or focused, unless the picker is open.
                shown.reactions.length === 0 &&
                  "opacity-0 group-hover/comment:opacity-100 group-focus-within/comment:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100",
              )}
            >
              <addReaction.definition.icon aria-hidden className="size-3.5" />
            </ToolbarButton>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-auto p-1">
            <ReactionPicker
              reacted={(content) =>
                shown.reactions.some(
                  (reaction) => reaction.content === content && reaction.viewer_reacted,
                )
              }
              onPick={(content) => {
                model.toggle(content);
                setPickerOpen(false);
              }}
            />
          </PopoverContent>
        </Popover>
      )}
      {shown.rereading && (
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          <Spinner className="size-3" aria-hidden />
          Checking GitHub…
        </span>
      )}
      {shown.unconfirmed && !shown.rereading && (
        <span role="status" className="text-xs text-muted-foreground">
          Couldn't read this comment's reactions from GitHub. They may be out of date.
        </span>
      )}
    </Toolbar>
  );
}

/** GitHub's eight reactions in a row; the arrows move between them, Enter or Space picks one. */
export function ReactionPicker({
  reacted,
  onPick,
}: {
  reacted: (content: ReactionContent) => boolean;
  onPick: (content: ReactionContent) => void;
}) {
  return (
    <Toolbar aria-label="Pick a reaction" loop className="flex items-center gap-0.5">
      {REACTION_CONTENTS.map((content) => {
        const { emoji, label } = REACTIONS[content];
        return (
          <ToolbarButton
            key={content}
            type="button"
            aria-label={label}
            title={label}
            aria-pressed={reacted(content)}
            data-reaction={content}
            className="inline-flex size-8 items-center justify-center rounded-md text-base outline-none transition-colors hover:bg-accent focus-visible:ring-3 focus-visible:ring-ring/50 aria-pressed:bg-primary/10"
            onClick={() => onPick(content)}
          >
            <span aria-hidden>{emoji}</span>
          </ToolbarButton>
        );
      })}
    </Toolbar>
  );
}
