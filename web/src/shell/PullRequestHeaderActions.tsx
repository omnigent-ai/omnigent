// A pull request's own header actions, beside its sections: the Pull Request Actions menu, which
// holds Refresh Pull Request, its links and its removals. Each runs the same command ⌘K and the
// list's row menus offer, so their labels and availability can't drift.

import { Loader2Icon, MoreHorizontalIcon, Trash2Icon, type LucideIcon } from "lucide-react";
import { Fragment } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  pullRequestRefreshFailureText,
  type PullRequestRefreshFailure,
} from "@/pullRequestPage/usePullRequestRefresh";
import {
  type CommandAvailability,
  isCommandAvailable,
  isCommandListed,
} from "@/store/paletteCommands";

import { PULL_REQUEST_COMMAND_ICONS } from "./pullRequestCommands/icons";
import type { PullRequestCommandSection } from "./pullRequestCommands/model";
import type { PullRequestInViewCommand } from "./pullRequestCommands/usePullRequestInViewCommands";

export const PULL_REQUEST_ACTIONS_LABEL = "Pull Request Actions";

/** The session's merged and closed pull requests' removal, as the header's menu offers it. */
export interface PullRequestHeaderRemoveFinished {
  menuLabel: string;
  availability: CommandAvailability;
  run: () => void;
}

/** The menu's groups, in order; a separator goes between each that has items. */
const MENU_SECTIONS: readonly PullRequestCommandSection[] = [
  "refresh",
  "link",
  "open",
  "copy",
  "remove",
];

interface MenuItem {
  id: string;
  section: PullRequestCommandSection;
  label: string;
  icon: LucideIcon;
  availability: CommandAvailability;
  run: () => void;
}

/** Why a command can't run now, for its tooltip; none when it can. */
function unavailableReason(availability: CommandAvailability): string | null {
  return availability.available ? null : (availability.reason ?? null);
}

export function PullRequestHeaderActions({
  commands,
  refreshing,
  removeFinished,
}: {
  /** The pull request's own commands, as ⌘K lists them for it while it's shown. */
  commands: readonly PullRequestInViewCommand[];
  refreshing: boolean;
  removeFinished: PullRequestHeaderRemoveFinished | null;
}) {
  const items: MenuItem[] = [
    ...commands
      .filter(({ command }) => MENU_SECTIONS.includes(command.section))
      .map(({ command, run }) => ({
        id: command.id,
        section: command.section,
        label: command.menuLabel,
        icon: PULL_REQUEST_COMMAND_ICONS[command.id],
        availability: command.availability,
        run,
      })),
    ...(removeFinished && isCommandListed(removeFinished.availability)
      ? [
          {
            id: "remove-finished",
            section: "remove" as const,
            label: removeFinished.menuLabel,
            // As its ⌘K row shows it.
            icon: Trash2Icon,
            availability: removeFinished.availability,
            run: removeFinished.run,
          },
        ]
      : []),
  ];
  const groups = MENU_SECTIONS.map((section) =>
    items.filter((item) => item.section === section),
  ).filter((group) => group.length > 0);
  // Disabled only when nothing in it can run, as the reference app's menu is.
  const menuDisabled = !items.some((item) => isCommandAvailable(item.availability));
  return (
    <>
      {/* Said for screen readers only, and out of the layout: the menu's own glyph shows it. */}
      {refreshing && (
        <span role="status" className="sr-only">
          Refreshing…
        </span>
      )}
      <DropdownMenu>
        <Tooltip>
          <TooltipTrigger asChild>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label={PULL_REQUEST_ACTIONS_LABEL}
                aria-busy={refreshing || undefined}
                disabled={menuDisabled}
                className="shrink-0"
              >
                {/* A refresh chosen from the menu shows that it's running in the menu's place. */}
                {refreshing ? (
                  <Loader2Icon
                    className="size-3.5 animate-spin text-muted-foreground"
                    aria-hidden
                  />
                ) : (
                  <MoreHorizontalIcon className="size-3.5" aria-hidden />
                )}
              </Button>
            </DropdownMenuTrigger>
          </TooltipTrigger>
          <TooltipContent>{PULL_REQUEST_ACTIONS_LABEL}</TooltipContent>
        </Tooltip>
        <DropdownMenuContent align="end">
          {groups.map((group, index) => (
            <Fragment key={group[0].section}>
              {index > 0 && <DropdownMenuSeparator />}
              {group.map((item) => {
                const Icon = item.icon;
                const reason = unavailableReason(item.availability);
                return (
                  <DropdownMenuItem
                    key={item.id}
                    disabled={!isCommandAvailable(item.availability)}
                    title={reason ?? undefined}
                    onSelect={item.run}
                  >
                    <Icon aria-hidden />
                    {item.label}
                  </DropdownMenuItem>
                );
              })}
            </Fragment>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
    </>
  );
}

/** The line under the header for a refresh that didn't go through, with a way to try again. */
export function PullRequestRefreshNotice({
  failure,
  refreshing,
  onTryAgain,
}: {
  failure: PullRequestRefreshFailure | null;
  refreshing: boolean;
  onTryAgain: () => void;
}) {
  if (!failure) return null;
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center gap-x-2 gap-y-1 px-4 py-1.5 text-xs text-destructive"
    >
      <p className="min-w-0">{pullRequestRefreshFailureText(failure)}</p>
      <Button
        type="button"
        variant="link"
        size="sm"
        className="h-auto p-0 text-xs"
        disabled={refreshing}
        onClick={onTryAgain}
      >
        Try Again
      </Button>
    </div>
  );
}
