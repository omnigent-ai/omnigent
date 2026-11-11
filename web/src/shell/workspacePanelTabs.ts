/**
 * Labels, icons and trigger presentation for the Workspace rail's fixed panel
 * tabs (Files, Changes, Pull Requests, Agents, Content search). `WorkspacePanel`
 * draws every trigger from these descriptors with one tab-agnostic renderer.
 */

import {
  BotIcon,
  FileDiffIcon,
  FolderTreeIcon,
  GitPullRequestIcon,
  type LucideIcon,
  SearchIcon,
} from "lucide-react";
import { ARIA_MOD_KEY, MOD_KEY, SHIFT_KEY } from "@/components/KeyboardShortcut";
import { cn } from "@/lib/utils";
import { type RightRailTab, TAB_BADGE_BASE } from "./railTabs";

/** The rail's fixed panel tabs: everything but the dynamic browser and side-chat modes. */
export type WorkspacePanelTab = Exclude<RightRailTab, "browser" | "sidechat">;

/** What a tab's trigger may depend on, gathered once per render by `WorkspacePanel`. */
export interface WorkspacePanelTabContext {
  pending: boolean;
  showFilesPanel: boolean;
  showGithubTab: boolean;
  showContentSearch: boolean;
  changedCount: number;
  agentCount: number;
  subagentsWorking: number;
  /** This tab's ⌃N digit among the visible fixed tabs, if it has one. */
  digit: string | undefined;
  /** `aria-keyshortcuts` for that digit. */
  digitAriaShortcut: string | undefined;
}

/** A trailing visually hidden badge, such as the Changes count. */
export interface WorkspacePanelTabBadge {
  text: string | number;
  className: string;
}

export interface WorkspacePanelTabDescriptor {
  label: string;
  icon: LucideIcon;
  /** Whether the tab strip draws this tab's trigger. */
  shown: (context: WorkspacePanelTabContext) => boolean;
  /** Defaults to `label`. */
  ariaLabel?: (context: WorkspacePanelTabContext) => string;
  /** Defaults to the ⌃N digit; an override returning `undefined` shows no shortcut. */
  tooltipShortcut?: (context: WorkspacePanelTabContext) => string | readonly string[] | undefined;
  /** Defaults to the ⌃N digit's chord; an override returning `undefined` omits it. */
  ariaKeyShortcuts?: (context: WorkspacePanelTabContext) => string | undefined;
  badge?: (context: WorkspacePanelTabContext) => WorkspacePanelTabBadge | null;
}

export const WORKSPACE_PANEL_TABS: Record<WorkspacePanelTab, WorkspacePanelTabDescriptor> = {
  "content-search": {
    label: "Content search",
    icon: SearchIcon,
    shown: ({ showContentSearch, showFilesPanel }) => showContentSearch && showFilesPanel,
    tooltipShortcut: () => [MOD_KEY, SHIFT_KEY, "F"],
    ariaKeyShortcuts: ({ digitAriaShortcut }) =>
      `${ARIA_MOD_KEY}+Shift+F ${digitAriaShortcut ?? ""}`.trim(),
  },
  files: {
    label: "Files",
    icon: FolderTreeIcon,
    shown: ({ pending, showFilesPanel }) => pending || showFilesPanel,
  },
  changes: {
    label: "Changes",
    icon: FileDiffIcon,
    shown: ({ pending, showFilesPanel }) => pending || showFilesPanel,
    ariaLabel: ({ changedCount }) =>
      changedCount > 0 ? `Changes ${changedCount} changed` : "Changes",
    badge: ({ changedCount }) =>
      changedCount > 0 ? { text: changedCount, className: "sr-only" } : null,
  },
  github: {
    label: "Pull Requests",
    icon: GitPullRequestIcon,
    shown: ({ pending, showGithubTab }) => pending || showGithubTab,
  },
  subagents: {
    label: "Agents",
    icon: BotIcon,
    shown: () => true,
    ariaLabel: ({ subagentsWorking, agentCount }) =>
      subagentsWorking > 0 ? `Agents ${subagentsWorking}/${agentCount}` : `Agents ${agentCount}`,
    badge: ({ subagentsWorking, agentCount }) => ({
      text: subagentsWorking > 0 ? `${subagentsWorking}/${agentCount}` : agentCount,
      className: cn(
        TAB_BADGE_BASE,
        "sr-only",
        subagentsWorking > 0 ? "text-success" : "text-muted-foreground",
      ),
    }),
  },
};

/** The resolved props one tab trigger draws. */
export interface WorkspacePanelTabPresentation {
  label: string;
  icon: LucideIcon;
  ariaLabel: string;
  tooltipShortcut: string | readonly string[] | undefined;
  ariaKeyShortcuts: string | undefined;
  badge: WorkspacePanelTabBadge | null;
}

/** Resolve a descriptor against the current context; `null` when it isn't drawn. */
export function workspacePanelTabPresentation(
  descriptor: WorkspacePanelTabDescriptor,
  context: WorkspacePanelTabContext,
): WorkspacePanelTabPresentation | null {
  if (!descriptor.shown(context)) return null;
  return {
    label: descriptor.label,
    icon: descriptor.icon,
    ariaLabel: descriptor.ariaLabel ? descriptor.ariaLabel(context) : descriptor.label,
    tooltipShortcut: descriptor.tooltipShortcut
      ? descriptor.tooltipShortcut(context)
      : context.digit,
    ariaKeyShortcuts: descriptor.ariaKeyShortcuts
      ? descriptor.ariaKeyShortcuts(context)
      : context.digitAriaShortcut,
    badge: descriptor.badge ? descriptor.badge(context) : null,
  };
}
