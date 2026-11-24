// The strip's tab list: the open workspace tabs grouped by kind, opened on the one on screen,
// with a fuzzy search over them. Each row closes its tab, and a group's heading closes the group.
// It opens from the layers button after ‹ › or from ⌘K's "Search tabs…", which share one command.

import { ChevronDownIcon, LayersIcon, type LucideIcon, RotateCcwIcon, XIcon } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { QuickPickGroupAction, QuickPickItem } from "@/components/ui/quick-pick/model";
import { QuickPick } from "@/components/ui/quick-pick/QuickPick";
import { type PullRequestStatus, pullRequestStatusPresentation } from "@/lib/pullRequestStatus";
import type { Chord } from "@/lib/shortcuts/chord";
import { cn } from "@/lib/utils";
import { FileTypeIcon } from "@/shell/FileTypeIcon";

import { SuppressBrowserView } from "@/hooks/useSuppressBrowserView";
import { WorkspaceTabTooltip } from "@/shell/WorkspacePanel";

import { TAB_GROUPS, tabGroup, type TabGroupId } from "./tabGroups";

export interface TabSearchEntry {
  tabId: string;
  label: string;
  /** The full name and place, such as a file's path or a page's address. */
  tooltip: string;
  icon: LucideIcon;
  /** A file tab shows its type's icon, as the strip does. */
  filePath?: string;
  selected: boolean;
  group: TabGroupId;
  /** The pull request a PR tab shows, from what the session already read of it. */
  pullRequest?: TabSearchPullRequest;
  /** The tab holds edits not yet saved. */
  unsaved?: boolean;
  /** Its row offers Close; false for a tab this user can't close. */
  closable: boolean;
}

export interface TabSearchPullRequest {
  /** Such as `#338`. */
  number?: string;
  /** The PR's title, for the PR's own tab; a tab on one of its files keeps the file's name. */
  title?: string;
  /** Who wrote it and where, such as `octocat · acme/repo`. */
  description?: string;
  status: PullRequestStatus;
}

export type TabSearchChoice = { kind: "tab"; tabId: string } | { kind: "reopen" };

export const TAB_ACTIONS_GROUP = "Actions";
/** Closes the active row's tab, as a browser's tab search does. */
export const CLOSE_TAB_CHORD: Chord = { mod: true, key: "Backspace" };

/** The list's Reopen row; null while no closed tab can come back, so the row isn't offered. */
export type TabSearchReopen = { label: string; shortcut?: readonly string[] } | null;

/** A PR row reads as its number and title, with who wrote it and where under them. */
function pullRequestText(entry: TabSearchEntry) {
  const pr = entry.pullRequest;
  if (!pr?.title) return null;
  return {
    label: [pr.number, pr.title].filter(Boolean).join(" "),
    description: pr.description,
  };
}

function TabRowAccessory({ entry }: { entry: TabSearchEntry }) {
  const status = entry.pullRequest?.status;
  const shownStatus = status === undefined || status === "unknown" ? undefined : status;
  if (!entry.selected && !entry.unsaved && shownStatus === undefined) return null;
  const presentation = shownStatus ? pullRequestStatusPresentation(shownStatus) : undefined;
  return (
    <span className="flex items-center gap-2">
      {entry.unsaved && (
        <span
          role="img"
          aria-label="Unsaved changes"
          title="Unsaved changes"
          className="size-2 rounded-full bg-foreground/70"
        />
      )}
      {presentation && (
        <span
          data-pull-request-status={shownStatus}
          className="inline-flex h-5 items-center gap-1 rounded-full border border-border px-1.5 text-xs text-muted-foreground"
        >
          <presentation.Icon aria-hidden className={cn("size-3", presentation.className)} />
          {presentation.label}
        </span>
      )}
      {entry.selected && <span className="text-sm text-muted-foreground">Current</span>}
    </span>
  );
}

/**
 * A group per kind of tab in `TAB_GROUPS` order, each in strip order, then the list's own action
 * when it can run. The tab on screen stays in its group, so a heading counts and closes what it
 * lists. `onClose` gives each closable row Close.
 */
export function tabSearchItems(
  entries: readonly TabSearchEntry[],
  reopen: TabSearchReopen,
  onClose?: (tabId: string) => void,
): QuickPickItem<TabSearchChoice>[] {
  const item = (entry: TabSearchEntry): QuickPickItem<TabSearchChoice> => {
    const pullRequest = pullRequestText(entry);
    const label = pullRequest?.label ?? entry.label;
    const description =
      pullRequest?.description ?? (entry.tooltip !== entry.label ? entry.tooltip : undefined);
    return {
      id: `tab:${entry.tabId}`,
      label,
      ...(description ? { description } : {}),
      ...(pullRequest ? { keywords: [entry.label, entry.tooltip] } : {}),
      icon: entry.filePath !== undefined ? <FileTypeIcon path={entry.filePath} /> : <entry.icon />,
      accessory: <TabRowAccessory entry={entry} />,
      group: tabGroup(entry.group).label,
      ...(onClose && entry.closable
        ? {
            actions: [
              {
                id: "close",
                label: "Close tab",
                icon: <XIcon />,
                chord: CLOSE_TAB_CHORD,
                run: () => onClose(entry.tabId),
              },
            ],
          }
        : {}),
      value: { kind: "tab", tabId: entry.tabId },
    };
  };
  const tabs = TAB_GROUPS.flatMap((group) =>
    entries.filter((entry) => entry.group === group.id),
  ).map(item);
  if (!reopen) return tabs;
  return [
    ...tabs,
    {
      id: "action:reopen",
      label: reopen.label,
      icon: <RotateCcwIcon />,
      group: TAB_ACTIONS_GROUP,
      keywords: ["reopen", "restore", "undo close"],
      ...(reopen.shortcut ? { shortcut: reopen.shortcut } : {}),
      value: { kind: "reopen" },
    },
  ];
}

/** What the list closes: one tab, and the buttons each group's heading shows. */
export interface TabSearchClosing {
  closeTab: (tabId: string) => void;
  groupActions: (group: TabGroupId) => readonly QuickPickGroupAction[];
}

export function WorkspaceTabSearch({
  open,
  onOpenChange,
  entries,
  reopen,
  closing,
  onChoose,
  trailing = false,
}: {
  /**
   * With floating panes the button ends the strip: it shows a chevron, and the list lines up
   * with its end. The strip that places it decides, so a drawer keeps the classic button.
   */
  trailing?: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  entries: readonly TabSearchEntry[];
  reopen: TabSearchReopen;
  closing?: TabSearchClosing;
  onChoose: (choice: TabSearchChoice) => void;
}) {
  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <WorkspaceTabTooltip label="All tabs">
        <PopoverTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            className="size-6 shrink-0 text-muted-foreground hover:text-foreground"
            aria-label="All tabs"
          >
            {/* Floating panes open the list from a chevron, as a menu of the tabs. */}
            {trailing ? <ChevronDownIcon className="size-4" /> : <LayersIcon className="size-4" />}
          </Button>
        </PopoverTrigger>
      </WorkspaceTabTooltip>
      <PopoverContent
        align={trailing ? "end" : "start"}
        className="w-[min(35rem,calc(100vw-2rem))] p-0"
      >
        <SuppressBrowserView />
        <TabSearchPicker
          entries={entries}
          reopen={reopen}
          closing={closing}
          onClose={() => onOpenChange(false)}
          onChoose={onChoose}
        />
      </PopoverContent>
    </Popover>
  );
}

/** Mounted with the open list, so each opening, from the layers button or ⌘K, starts on the tab on screen. */
function TabSearchPicker({
  entries,
  reopen,
  closing,
  onClose,
  onChoose,
}: {
  entries: readonly TabSearchEntry[];
  reopen: TabSearchReopen;
  closing?: TabSearchClosing;
  onClose: () => void;
  onChoose: (choice: TabSearchChoice) => void;
}) {
  const [query, setQuery] = useState("");
  const [activeId, setActiveId] = useState<string | null>(() => {
    const selected = entries.find((entry) => entry.selected);
    return selected ? `tab:${selected.tabId}` : null;
  });
  return (
    <QuickPick<TabSearchChoice>
      label="Search tabs"
      placeholder="Search tabs by title, number, path or address"
      segments={[
        {
          sourceId: "tabs",
          ranking: "client",
          items: tabSearchItems(entries, reopen, closing?.closeTab),
        },
      ]}
      groupCounts
      groupActions={(label) => {
        const group = TAB_GROUPS.find((candidate) => candidate.label === label);
        return group && closing ? closing.groupActions(group.id) : [];
      }}
      acceptLabel="Switch"
      onClose={onClose}
      query={query}
      onQueryChange={(next) => {
        setQuery(next);
        setActiveId(null);
      }}
      autoFocus
      loop
      activeId={activeId}
      onActiveIdChange={setActiveId}
      emptyMessage="No open tab matches."
      onAccept={(item) => {
        onClose();
        onChoose(item.value);
      }}
      className="rounded-md"
    />
  );
}
