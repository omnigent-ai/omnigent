// The Workspace settings' pinned pages and first page, as a session's tab strip takes them:
// all at once for a strip that never took them, and afterwards only what a setting changed,
// so a pinned page the user closed stays closed until its setting changes again.

import { useMemo, useSyncExternalStore } from "react";

import {
  type DefaultWorkspaceTab,
  isDefaultWorkspaceTab,
  readInitialWorkspaceTab,
  readPinnedWorkspaceTabs,
  subscribeWorkspaceTabPreferences,
} from "@/lib/workspaceTabPreferences";

import type { WorkspacePlace } from "./workspacePlace";

export interface WorkspaceTabSettings {
  /** The pinned pages, in Settings order. */
  pinned: readonly DefaultWorkspaceTab[];
  /** The page a strip shows first: the default page when it's pinned, else the first pinned. */
  initial: DefaultWorkspaceTab | null;
}

/** What a strip last took from the settings, saved with the strip. */
export interface AppliedTabSettings {
  pinned: DefaultWorkspaceTab[];
  initial: DefaultWorkspaceTab | null;
}

/** What a strip does to follow the settings, and what it then has taken. */
export interface TabSettingsChange {
  /** Pages to show as pinned tabs, opening those that aren't open. */
  pin: DefaultWorkspaceTab[];
  /** Pages whose tabs lose their pin and stay open. */
  unpin: DefaultWorkspaceTab[];
  /** The page to select, once, when the first page changed. */
  select: DefaultWorkspaceTab | null;
  applied: AppliedTabSettings;
}

export function readWorkspaceTabSettings(): WorkspaceTabSettings {
  return { pinned: readPinnedWorkspaceTabs(), initial: readInitialWorkspaceTab() };
}

// One string per settings value, so the hook's snapshot is stable between changes.
const settingsKey = () => {
  const { pinned, initial } = readWorkspaceTabSettings();
  return `${pinned.join(",")}|${initial ?? ""}`;
};

/** The settings as they are now, following changes in Settings and in other windows. */
export function useWorkspaceTabSettings(): WorkspaceTabSettings {
  const key = useSyncExternalStore(subscribeWorkspaceTabPreferences, settingsKey, settingsKey);
  return useMemo(() => {
    const [pinned, initial] = key.split("|");
    return {
      pinned: pinned.split(",").filter(isDefaultWorkspaceTab),
      initial: isDefaultWorkspaceTab(initial) ? initial : null,
    };
  }, [key]);
}

/** The place a settings page shows as a tab. */
export function settingsPagePlace(page: DefaultWorkspaceTab): WorkspacePlace {
  switch (page) {
    case "files":
      return { kind: "files" };
    case "changes":
      return { kind: "changes" };
    case "github":
      return { kind: "pulls" };
    case "subagents":
      return { kind: "agents" };
    case "tasks":
      return { kind: "tasks" };
  }
}

/** What a strip is like as it follows the settings. */
export interface TabSettingsStrip {
  /** Pages the session can show now. */
  canShow: (page: DefaultWorkspaceTab) => boolean;
  /** Pages the strip has room to open as new tabs; the rest wait for room. */
  canOpen: (page: DefaultWorkspaceTab) => boolean;
  /** The strip shows only its empty New tab, so nothing was chosen yet. */
  untouched: boolean;
}

/**
 * How a strip that last took `applied` (none for a new or older strip) follows `settings`.
 * A pinned page the session can't show or open yet is left out of `applied`, so it's pinned
 * later. The first page is selected only as the strip opens or when the setting changes,
 * and only if the session can show it then; otherwise the selection stays, and the page
 * never takes it later. A strip that already shows something, such as a file a link opened,
 * keeps it.
 */
export function tabSettingsChange(
  settings: WorkspaceTabSettings,
  applied: AppliedTabSettings | undefined,
  { canShow, canOpen, untouched }: TabSettingsStrip,
): TabSettingsChange {
  const before = applied?.pinned ?? [];
  const pin = settings.pinned.filter(
    (page) => !before.includes(page) && canShow(page) && canOpen(page),
  );
  const unpin = before.filter((page) => !settings.pinned.includes(page));
  const initialChanged = applied === undefined || applied.initial !== settings.initial;
  const keepsSelection = applied === undefined && !untouched;
  const select =
    initialChanged &&
    !keepsSelection &&
    settings.initial !== null &&
    canShow(settings.initial) &&
    canOpen(settings.initial)
      ? settings.initial
      : null;
  return {
    pin,
    unpin,
    select,
    applied: {
      pinned: settings.pinned.filter((page) => before.includes(page) || pin.includes(page)),
      initial: settings.initial,
    },
  };
}

export function sameAppliedTabSettings(
  left: AppliedTabSettings | undefined,
  right: AppliedTabSettings,
): boolean {
  return (
    left !== undefined &&
    left.initial === right.initial &&
    left.pinned.length === right.pinned.length &&
    left.pinned.every((page, index) => right.pinned[index] === page)
  );
}

/** The saved record in `value`, or undefined for a missing or malformed one. */
export function parseAppliedTabSettings(value: unknown): AppliedTabSettings | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  if (!Array.isArray(record.pinned)) return undefined;
  const pinned = [
    ...new Set(
      record.pinned.filter((page): page is DefaultWorkspaceTab =>
        typeof page === "string" ? isDefaultWorkspaceTab(page) : false,
      ),
    ),
  ];
  const initial =
    typeof record.initial === "string" && isDefaultWorkspaceTab(record.initial)
      ? record.initial
      : null;
  return { pinned, initial };
}
