// The page an empty workspace tab shows: the workspace's apps as cards over a
// filterable, keyboard-driven list. Picking one turns the tab into that app; the tab store
// decides how (show a place, or run a command such as starting a shell). The
// shell chooser is part of the tab's place (`omnigent://new?app=terminal`), so
// links, restore and Back can open it, and opening it updates the tab.

import {
  BotIcon,
  FolderTreeIcon,
  GitCompareIcon,
  GitPullRequestIcon,
  ListChecksIcon,
  GlobeIcon,
  MessageSquarePlusIcon,
  SquareTerminalIcon,
  TextSearchIcon,
  type LucideIcon,
} from "lucide-react";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import type { QuickPickItem } from "@/components/ui/quick-pick/model";
import { QuickPick } from "@/components/ui/quick-pick/QuickPick";
import type { SandboxResumeStatus } from "@/lib/sandboxResume";
import { shortcutKeys } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import type { WorkspaceCommand } from "@/shell/workspaceCommands/model";

import {
  newTabEntries,
  shellChoiceEntries,
  type NewTabAction,
  type NewTabEntry,
} from "./newTabEntries";
import type { WorkspacePlace } from "./workspacePlace";

const ICONS: Record<string, LucideIcon> = {
  terminal: SquareTerminalIcon,
  "choose-shell": SquareTerminalIcon,
  files: FolderTreeIcon,
  changes: GitCompareIcon,
  pulls: GitPullRequestIcon,
  agents: BotIcon,
  tasks: ListChecksIcon,
  "side-chat": MessageSquarePlusIcon,
  browser: GlobeIcon,
  search: TextSearchIcon,
};

export type NewTabPlace = Extract<WorkspacePlace, { kind: "new" }>;

/** A place to go back to, as the page lists it under Recent. */
export interface NewTabRecent {
  /** The place's identity; the row's ID is `recent:<identity>`. */
  identity: string;
  label: string;
  /** Quiet text after the label, such as a file's folder. */
  description?: string;
  /** Kept at the trailing edge when the row is tight, such as a pull request's number. */
  detail?: string;
  /** Hidden terms that also match, such as a pull request's author. */
  keywords?: readonly string[];
  icon: ReactNode;
}

/** The apps shown as cards, most used from an empty tab first; the rest are rows under More. */
const CARD_APPS: readonly string[] = ["pulls", "side-chat", "changes", "files"];

/** A card's hint; the terminal's names the default shell, and says when it waits on the sandbox. */
function appHint(entry: NewTabEntry, sandbox: SandboxResumeStatus): string | undefined {
  switch (entry.id) {
    case "terminal": {
      const shell = entry.description ?? "a shell";
      if (sandbox === "resuming") return `Opens ${shell} once the sandbox resumes`;
      if (sandbox === "asleep" || sandbox === "failed") {
        return `Resumes the sandbox, then opens ${shell}`;
      }
      return entry.description ? `Run commands in ${entry.description}` : "Run commands";
    }
    case "side-chat":
      return "Ask a quick question on the side";
    case "changes":
      return "Review local changes before pushing";
    case "files":
      return "Browse and open the workspace's files";
    case "pulls":
      return "Review the pull requests for this workspace";
    default:
      return entry.description;
  }
}

type PageAction = NewTabAction | { kind: "recent"; identity: string };

const RECENT_PREFIX = "recent:";

export interface NewTabPageProps {
  /** The tab's place: the app list, or with `app: "terminal"` the shell chooser. */
  place: NewTabPlace;
  /** Opening or leaving the shell chooser, so the tab's place and title follow. */
  onPlaceChange: (place: NewTabPlace) => void;
  /** The workspace's open commands, as the "+" menu and ⌘K list them. */
  commands: readonly WorkspaceCommand[];
  /** Whether "Search text" can open the workspace text search on this session. */
  textSearch: boolean;
  /** Open the workspace text search; this tab stays as it is. */
  onTextSearch: () => void;
  /** Show `place` in this tab, such as the Files tree or a blank browser page. */
  onOpenPlace: (place: WorkspacePlace) => void;
  /** Run an app's command in this tab's place, such as starting a shell. */
  onRunCommand: (command: WorkspaceCommand) => void;
  /** Places to go back to, newest first; the Recent section hides when there are none. */
  recents?: readonly NewTabRecent[];
  /** Go to a recent place; this tab gives way to it. */
  onOpenRecent?: (identity: string) => void;
  /** Escape on the app list, such as closing the empty tab. */
  onDismiss?: () => void;
  /** The session's sandbox, when it's stopped: Terminal says it waits on it. */
  sandbox?: SandboxResumeStatus;
  /** Shown above the apps, such as the sandbox's Resume notice. */
  notice?: ReactNode;
  autoFocus?: boolean;
}

interface PageState {
  query: string;
  activeId: string | null;
}

const FRESH_PAGE: PageState = { query: "", activeId: null };

function toItem(
  entry: NewTabEntry,
  grouped = false,
  sandbox: SandboxResumeStatus = "none",
): QuickPickItem<PageAction> {
  const Icon = ICONS[entry.id.startsWith("shell:") ? "terminal" : entry.id] ?? SquareTerminalIcon;
  const shortcut = entry.shortcut ? shortcutKeys(entry.shortcut) : undefined;
  return {
    id: entry.id,
    label: entry.label,
    ...(entry.description ? { description: entry.description } : {}),
    keywords: entry.keywords,
    icon: <Icon aria-hidden className="size-4 text-muted-foreground" />,
    ...(shortcut ? { shortcut } : {}),
    ...(entry.disabledReason ? { disabledReason: entry.disabledReason } : {}),
    ...(entry.action.kind === "choose-shell" ? { opensPage: true } : {}),
    ...(grouped && CARD_APPS.includes(entry.id)
      ? { group: "Apps", layout: "card" as const, description: appHint(entry, sandbox) }
      : grouped
        ? { group: "More" }
        : {}),
    rememberInHistory: false,
    value: entry.action,
  };
}

function recentItem(recent: NewTabRecent): QuickPickItem<PageAction> {
  return {
    id: `${RECENT_PREFIX}${recent.identity}`,
    label: recent.label,
    ...(recent.description ? { description: recent.description } : {}),
    ...(recent.detail
      ? { accessory: <span className="text-sm text-muted-foreground">{recent.detail}</span> }
      : {}),
    ...(recent.keywords ? { keywords: recent.keywords } : {}),
    icon: recent.icon,
    group: "Recent",
    value: { kind: "recent", identity: recent.identity },
  };
}

export function NewTabPage({
  place,
  onPlaceChange,
  commands,
  textSearch,
  onTextSearch,
  onOpenPlace,
  onRunCommand,
  recents,
  onOpenRecent,
  onDismiss,
  sandbox = "none",
  notice,
  autoFocus = true,
}: NewTabPageProps) {
  const onShells = place.app === "terminal";
  // Each page keeps its own query and active row, so Back restores the app list as it was.
  const [apps, setApps] = useState<PageState>(FRESH_PAGE);
  const [shells, setShells] = useState<PageState>(FRESH_PAGE);
  const current = onShells ? shells : apps;
  const setCurrent = onShells ? setShells : setApps;

  const rootEntries = useMemo(
    () => newTabEntries({ commands, textSearch }),
    [commands, textSearch],
  );
  const items = useMemo(() => {
    if (onShells) return shellChoiceEntries(commands).map((entry) => toItem(entry));
    // The app cards first, then the other apps, then recent places when there are any.
    const appItems = rootEntries.map((entry) => toItem(entry, true, sandbox));
    return [
      ...appItems.filter((item) => item.group === "Apps"),
      ...appItems.filter((item) => item.group === "More"),
      ...(recents ?? []).map(recentItem),
    ];
  }, [commands, onShells, recents, rootEntries, sandbox]);
  // Every shell is a row of its own or behind "Choose shell…"; Right Arrow on Terminal lists them all.
  const canChooseShell =
    shellChoiceEntries(commands).length > 1 &&
    rootEntries.some((entry) => entry.id === "terminal" && !entry.disabledReason);
  const pageRef = useRef<HTMLDivElement>(null);
  const queryFieldRef = useRef<HTMLInputElement | null>(null);
  // Moving between the apps and the chooser keeps typing in the field, unless focus is elsewhere.
  const shownPage = useRef(onShells);
  useEffect(() => {
    if (shownPage.current === onShells) return;
    shownPage.current = onShells;
    const focused = document.activeElement;
    if (!focused || focused === document.body || pageRef.current?.contains(focused)) {
      queryFieldRef.current?.focus();
    }
  }, [onShells]);

  const openChooser = () => {
    setShells(FRESH_PAGE);
    onPlaceChange({ kind: "new", app: "terminal" });
  };
  const backToApps = () => onPlaceChange({ kind: "new" });
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 p-3" data-testid="workspace-new-tab-page">
      {notice}
      <div ref={pageRef} className="flex min-h-0 flex-1 flex-col">
        <QuickPick<PageAction>
          label={onShells ? "Choose a shell" : "Open an app in this tab"}
          placeholder={onShells ? "Choose a shell…" : "Search apps and recent places…"}
          backLabel="Back to apps"
          segments={[
            {
              sourceId: "new-tab",
              ranking: "client",
              groupOrder: "match",
              fuzzyMatching: "words",
              items,
            },
          ]}
          query={current.query}
          onQueryChange={(query) => setCurrent({ query, activeId: null })}
          activeId={current.activeId}
          onActiveIdChange={(activeId) => setCurrent((page) => ({ ...page, activeId }))}
          emptyMessage={onShells ? "No shell matches." : "No app or recent place matches."}
          matchHighlight="underline"
          autoFocus={autoFocus}
          fillHeight
          inputRef={queryFieldRef}
          onBack={onShells ? backToApps : undefined}
          onDismiss={onShells ? undefined : onDismiss}
          // Right Arrow on Terminal shows every shell, as the chooser page does.
          onExpand={(item) => {
            if (onShells || !canChooseShell || item.id !== "terminal") return false;
            openChooser();
            return true;
          }}
          onAccept={(item) => {
            const action = item.value;
            if (action.kind === "choose-shell") {
              openChooser();
              return;
            }
            if (action.kind === "recent") onOpenRecent?.(action.identity);
            else if (action.kind === "place") onOpenPlace(action.place);
            else if (action.kind === "text-search") onTextSearch();
            else onRunCommand(action.command);
          }}
        />
      </div>
    </div>
  );
}
