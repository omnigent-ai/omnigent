// A review conversation's Resolve or Unresolve, where a thread shows it: the button beside its
// reply box, its header's ⋯ menu, and ⌘K for the conversation worked in last. All three run the
// one command `useReviewThreadResolution` gives.

import { useId, useMemo, useRef } from "react";
import { CircleCheckIcon, CircleDotIcon, EllipsisIcon, Loader2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

import type { ThreadResolutionCommand } from "./useReviewThreadResolution";

const NO_COMMANDS: PaletteCommand[] = [];

const pendingLabel = (command: ThreadResolutionCommand) =>
  command.resolves ? "Resolving…" : "Unresolving…";

/** The button by the reply box: Resolve Conversation, or Unresolve once resolved. */
export function ReviewThreadResolveButton({
  command,
  className,
}: {
  command: ThreadResolutionCommand;
  className?: string;
}) {
  const Icon = command.resolves ? CircleCheckIcon : CircleDotIcon;
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className={className}
      disabled={command.pending}
      onClick={command.run}
    >
      {command.pending ? (
        <Loader2Icon aria-hidden className="animate-spin" />
      ) : (
        <Icon aria-hidden />
      )}
      {command.pending ? pendingLabel(command) : command.label}
    </Button>
  );
}

/** The conversation's own ⋯ in its header. */
export function ReviewThreadMenu({
  command,
  className,
}: {
  command: ThreadResolutionCommand;
  className?: string;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Conversation actions"
          className={cn("text-muted-foreground", className)}
        >
          <EllipsisIcon aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem disabled={command.pending} onSelect={command.run}>
          {command.resolves ? <CircleCheckIcon aria-hidden /> : <CircleDotIcon aria-hidden />}
          {command.pending ? pendingLabel(command) : command.label}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** ⌘K's row for the conversation worked in last, while `active` says it's this one. */
export function useReviewThreadResolutionPalette(
  threadId: string,
  command: ThreadResolutionCommand | null,
  active: boolean,
): void {
  // Each view of a thread has its own rows: the same thread may show in two places at once.
  const source = `review-thread-resolution:${useId()}`;
  const latest = useRef(command);
  latest.current = command;
  const offered = active && !!command && !command.pending;
  const label = command?.label;
  const resolves = command?.resolves;
  const rows = useMemo<PaletteCommand[]>(
    () =>
      offered && label
        ? [
            {
              id: `review-thread:${threadId}:${resolves ? "resolve" : "unresolve"}`,
              label,
              icon: resolves ? CircleCheckIcon : CircleDotIcon,
              keywords: ["conversation", "thread", "review", resolves ? "resolve" : "reopen"],
              run: () => latest.current?.run(),
            },
          ]
        : NO_COMMANDS,
    [offered, label, resolves, threadId],
  );
  usePaletteCommands(source, "Conversation", rows);
}
