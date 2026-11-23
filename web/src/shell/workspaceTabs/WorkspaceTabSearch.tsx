// The strip's tab list: a fuzzy search over the open workspace tabs, opened from the layers button
// after ‹ › or from ⌘K's "Search tabs…", which share one command.

import { LayersIcon, type LucideIcon, RotateCcwIcon } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { QuickPickItem } from "@/components/ui/quick-pick/model";
import { QuickPick } from "@/components/ui/quick-pick/QuickPick";
import { FileTypeIcon } from "@/shell/FileTypeIcon";

import { SuppressBrowserView } from "@/hooks/useSuppressBrowserView";
import { WorkspaceTabTooltip } from "@/shell/WorkspacePanel";

export interface TabSearchEntry {
  tabId: string;
  label: string;
  /** The full name and place, such as a file's path or a page's address. */
  tooltip: string;
  icon: LucideIcon;
  /** A file tab shows its type's icon, as the strip does. */
  filePath?: string;
  selected: boolean;
}

export type TabSearchChoice = { kind: "tab"; tabId: string } | { kind: "reopen" };

export const OPEN_TABS_GROUP = "Open tabs";
export const TAB_ACTIONS_GROUP = "Actions";

/** The list's Reopen row; null while no closed tab can come back, so the row isn't offered. */
export type TabSearchReopen = { label: string; shortcut?: readonly string[] } | null;

/** One row per open tab, in strip order, then the list's own action when it can run. */
export function tabSearchItems(
  entries: readonly TabSearchEntry[],
  reopen: TabSearchReopen,
): QuickPickItem<TabSearchChoice>[] {
  const tabs = entries.map((entry): QuickPickItem<TabSearchChoice> => ({
    id: `tab:${entry.tabId}`,
    label: entry.label,
    ...(entry.tooltip !== entry.label ? { description: entry.tooltip } : {}),
    icon: entry.filePath !== undefined ? <FileTypeIcon path={entry.filePath} /> : <entry.icon />,
    ...(entry.selected
      ? { accessory: <span className="text-sm text-muted-foreground">Current</span> }
      : {}),
    group: OPEN_TABS_GROUP,
    value: { kind: "tab", tabId: entry.tabId },
  }));
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

export function WorkspaceTabSearch({
  open,
  onOpenChange,
  entries,
  reopen,
  onChoose,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  entries: readonly TabSearchEntry[];
  reopen: TabSearchReopen;
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
            <LayersIcon className="size-4" />
          </Button>
        </PopoverTrigger>
      </WorkspaceTabTooltip>
      <PopoverContent align="start" className="w-[min(26rem,calc(100vw-2rem))] p-0">
        <SuppressBrowserView />
        <TabSearchPicker
          entries={entries}
          reopen={reopen}
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
  onClose,
  onChoose,
}: {
  entries: readonly TabSearchEntry[];
  reopen: TabSearchReopen;
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
      placeholder="Search tabs"
      segments={[{ sourceId: "tabs", ranking: "client", items: tabSearchItems(entries, reopen) }]}
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
