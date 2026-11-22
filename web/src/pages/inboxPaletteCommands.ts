// With quick_pick_palette on, ⌘K offers the Inbox's actions: its tabs as a setting, and each
// visible card's open, mark-read and reply actions, run by the same handlers as the card's buttons.

import {
  CheckIcon,
  ExternalLinkIcon,
  FilterIcon,
  type LucideIcon,
  MessageSquareIcon,
  MessageSquareReplyIcon,
} from "lucide-react";
import { useMemo, useRef } from "react";

import type { InboxFilter } from "@/lib/inboxFilterPreferences";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
import { useQuickPickPaletteCommands } from "@/shell/screenCommands/useQuickPickPaletteCommands";
import {
  AVAILABLE,
  type CommandAvailability,
  type PaletteCommand,
  unavailableBecause,
} from "@/store/paletteCommands";
import { settingCommand } from "@/store/paletteSettings";

import type { InboxEntry } from "./InboxPage";

export const INBOX_FILTER_SETTING_LABEL = "Inbox filter";
/** Why an unread card's actions wait, as its buttons do while a reply sends. */
export const REPLY_SENDING_REASON = "Wait for the reply to send.";

export type InboxCardActionKind = "open" | "mark-read" | "reply";

export interface InboxCardAction {
  /** Stable for the action, its card, the session and the card's version, so a stale ⌘K row does nothing. */
  id: string;
  kind: InboxCardActionKind;
  label: string;
  keywords: readonly string[];
  availability: CommandAvailability;
  /** Where Open goes. */
  href?: string;
  /** The card it acts on. */
  entry: InboxEntry;
  /** It focuses something on the page once ⌘K has closed. */
  movesFocus?: boolean;
}

export interface InboxCardContext {
  /** The automation page exists, so a failure opens it rather than the list. */
  taskDetailsEnabled: boolean;
  /** A reply to this session is on its way. */
  replySending: (sessionId: string) => boolean;
  /** The card shows its reply field: the viewer can edit the session and no answer is pending. */
  canReply: (sessionId: string) => boolean;
}

const ICONS: Record<InboxCardActionKind, LucideIcon> = {
  open: ExternalLinkIcon,
  "mark-read": CheckIcon,
  reply: MessageSquareReplyIcon,
};

function actionId(
  kind: InboxCardActionKind,
  entry: InboxEntry,
  target: string,
  version: number | null = null,
): string {
  return `inbox-${kind}:${JSON.stringify([entry.key, target, version])}`;
}

/** The card's ⌘K actions, as its buttons offer them; suggestions offer none here. */
export function inboxCardActions(entry: InboxEntry, context: InboxCardContext): InboxCardAction[] {
  switch (entry.kind) {
    case "suggestion":
      return [];
    case "automation": {
      const { item } = entry;
      const name = `“${item.taskName}”`;
      return [
        {
          id: actionId("open", entry, item.taskId),
          kind: "open",
          label: `Open automation ${name}`,
          keywords: ["inbox", "automation", "failed", item.taskName],
          availability: AVAILABLE,
          href: context.taskDetailsEnabled ? `/tasks/${item.taskId}` : "/tasks",
          entry,
        },
        {
          id: actionId("mark-read", entry, item.taskId),
          kind: "mark-read",
          label: `Mark failed ${name} as read`,
          keywords: ["inbox", "automation", "failed", "read", item.taskName],
          availability: AVAILABLE,
          entry,
        },
      ];
    }
    case "approval": {
      const sessionId = entry.item.row.id;
      const title = conversationDisplayLabel(entry.item.row);
      return [
        {
          id: actionId("open", entry, sessionId),
          kind: "open",
          label: `Open “${title}” to respond`,
          keywords: ["inbox", "approval", "question", "session", title],
          availability: AVAILABLE,
          href: `/c/${sessionId}`,
          entry,
        },
      ];
    }
    case "comment": {
      const sessionId = entry.item.row.id;
      const title = conversationDisplayLabel(entry.item.row);
      const { comment } = entry.item;
      return [
        {
          id: actionId("open", entry, sessionId),
          kind: "open",
          label: comment.anchor_metadata ? `Open “${title}”` : `Open file in “${title}”`,
          keywords: ["inbox", "comment", "file", comment.path, title],
          availability: AVAILABLE,
          // As the card's link: the file viewer opens on the comment, which marks it seen.
          href: comment.anchor_metadata
            ? `/c/${sessionId}`
            : `/c/${sessionId}?file=${encodeURIComponent(comment.path)}&comment=${encodeURIComponent(comment.id)}`,
          entry,
        },
      ];
    }
    case "unread": {
      const sessionId = entry.item.row.id;
      const title = conversationDisplayLabel(entry.item.row);
      const availability = context.replySending(sessionId)
        ? unavailableBecause(REPLY_SENDING_REASON)
        : AVAILABLE;
      const actions: InboxCardAction[] = [
        {
          // Versioned, so a row listed before newer output can't mark that output read.
          id: actionId("open", entry, sessionId, entry.item.row.updated_at),
          kind: "open",
          label: `Open “${title}”`,
          keywords: ["inbox", "unread", "session", title],
          availability,
          href: `/c/${sessionId}`,
          entry,
        },
        {
          id: actionId("mark-read", entry, sessionId, entry.item.row.updated_at),
          kind: "mark-read",
          label: `Mark “${title}” as read`,
          keywords: ["inbox", "unread", "read", "seen", title],
          availability,
          entry,
        },
      ];
      if (context.canReply(sessionId)) {
        actions.push({
          id: actionId("reply", entry, sessionId, entry.item.row.updated_at),
          kind: "reply",
          label: `Reply to “${title}”…`,
          keywords: ["inbox", "unread", "reply", "message", "send", "answer", title],
          availability,
          entry,
          movesFocus: true,
        });
      }
      return actions;
    }
  }
}

export interface InboxPaletteHandlers {
  chooseFilter: (filter: InboxFilter) => void;
  navigate: (href: string) => void;
  /** The unread card's Mark as read, which also drops its toggle and reply state. */
  markUnreadRead: (entry: Extract<InboxEntry, { kind: "unread" }>) => void;
  markAutomationRead: (runId: string) => void;
  /** Expands the unread card and focuses its reply field. */
  replyTo: (entry: Extract<InboxEntry, { kind: "unread" }>) => void;
}

/** Run `action` as the card's button does. */
export function runInboxCardAction(action: InboxCardAction, handlers: InboxPaletteHandlers): void {
  const { entry } = action;
  if (action.kind === "reply") {
    if (entry.kind === "unread") handlers.replyTo(entry);
    return;
  }
  if (action.kind === "open") {
    // Opening an unread session is reading it, as the card's Open session does.
    if (entry.kind === "unread") handlers.markUnreadRead(entry);
    if (action.href) handlers.navigate(action.href);
    return;
  }
  if (entry.kind === "unread") handlers.markUnreadRead(entry);
  else if (entry.kind === "automation") handlers.markAutomationRead(entry.item.runId);
}

export interface InboxPaletteState extends InboxCardContext {
  filter: InboxFilter;
  /** The tabs the page shows. */
  tabs: readonly { value: InboxFilter; label: string }[];
  /** The cards the page shows, in order. */
  entries: readonly InboxEntry[];
}

/** The Inbox's ⌘K commands; `handlers` is read when a command runs. */
export function inboxPaletteCommands(
  state: InboxPaletteState,
  handlers: { current: InboxPaletteHandlers },
  latestActions: { current: readonly InboxCardAction[] },
): PaletteCommand[] {
  const commands: PaletteCommand[] = [
    settingCommand({
      id: "inbox-filter",
      owner: "inbox",
      label: INBOX_FILTER_SETTING_LABEL,
      icon: FilterIcon,
      keywords: ["inbox", "filter", "tab", "show"],
      current: state.filter,
      choices: state.tabs.map(({ value, label }) => ({ value, label })),
      choose: (value) => {
        const tab = state.tabs.find((candidate) => candidate.value === value);
        if (tab) handlers.current.chooseFilter(tab.value);
      },
    }),
  ];
  for (const action of state.entries.flatMap((entry) => inboxCardActions(entry, state))) {
    commands.push({
      id: action.id,
      label: action.label,
      icon: action.entry.kind === "comment" ? MessageSquareIcon : ICONS[action.kind],
      keywords: [...action.keywords],
      availability: action.availability,
      movesFocus: action.movesFocus,
      run: () => {
        // A row accepted after its card changed or left does nothing.
        const current = latestActions.current.find(({ id }) => id === action.id);
        if (current?.availability.available) runInboxCardAction(current, handlers.current);
      },
    });
  }
  return commands;
}

/** What the commands show, so a re-render with the same cards reuses them. */
function inboxSignature(state: InboxPaletteState, actions: readonly InboxCardAction[]): string {
  return JSON.stringify([
    state.filter,
    state.tabs,
    actions.map(({ id, label, availability, href, movesFocus }) => [
      id,
      label,
      availability,
      href ?? "",
      movesFocus === true,
    ]),
  ]);
}

/** Renders nothing; registers the Inbox's commands while the page shows. */
export function InboxPaletteCommands({
  handlers,
  ...state
}: InboxPaletteState & { handlers: InboxPaletteHandlers }): null {
  // The page rebuilds its entries and handlers each render; commands read the newest.
  const actions = state.entries.flatMap((entry) => inboxCardActions(entry, state));
  const latestState = useRef(state);
  latestState.current = state;
  const latestHandlers = useRef(handlers);
  latestHandlers.current = handlers;
  const latestActions = useRef(actions);
  latestActions.current = actions;
  const signature = inboxSignature(state, actions);
  const commands = useMemo(
    () => inboxPaletteCommands(latestState.current, latestHandlers, latestActions),
    // The signature captures everything the commands show.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [signature],
  );
  useQuickPickPaletteCommands("inbox", "Inbox", commands);
  return null;
}
