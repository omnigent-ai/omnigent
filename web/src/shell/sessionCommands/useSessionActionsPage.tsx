// ⌘K's page of one session's actions, opened with ⌘↵ on its row. It reads the session's
// facts live, so a row greys out or goes once it stops applying, and an accept hands the
// action to `SessionActionHost` only if it still applies then.

import { useContext, useRef } from "react";
import { useStore } from "zustand";

import type { QuickPickAcceptResult, QuickPickItem } from "@/components/ui/quick-pick";
import type { Conversation } from "@/hooks/useConversations";
import { SidebarDataContext } from "@/hooks/useSidebarData";
import { isCommandListed } from "@/store/paletteCommands";

import {
  leavePaletteChildPage,
  openPaletteChildPage,
  paletteChildPageStore,
  type PaletteParentPlace,
  rememberPaletteParent,
} from "../commandPaletteChildPage";
import type { PaletteTarget } from "../paletteTarget";
import { SESSION_COMMAND_ICONS } from "./icons";
import { type SessionCommandId, sessionCommands } from "./model";
import { requestSessionAction } from "./sessionActionStore";
import { useSessionCommandFacts } from "./useSessionCommandFacts";

export interface SessionActionsPage {
  open: boolean;
  heading: string | null;
  items: QuickPickItem<PaletteTarget>[];
  /** ⌘↵ on `conversation`'s row: list its actions, remembering where Back returns. */
  openActions: (conversation: Conversation, title: string, parent: PaletteParentPlace) => void;
  acceptAction: (command: SessionCommandId) => QuickPickAcceptResult;
  back: () => PaletteParentPlace | null;
}

export function useSessionActionsPage(): SessionActionsPage {
  const storePage = useStore(paletteChildPageStore, (state) => state.page);
  const page = storePage?.kind === "session-actions" ? storePage : null;
  // The sidebar's row once it loads newer state, such as a pin or a rename, else the listed one.
  const loadedRows = useContext(SidebarDataContext)?.loadedRows;
  const conversation = page
    ? (loadedRows?.find((row) => row.id === page.sessionId) ?? page.conversation)
    : null;
  const facts = useSessionCommandFacts(conversation);
  const commands = facts
    ? sessionCommands(facts).filter((command) => isCommandListed(command.availability))
    : [];
  const latest = useRef({ conversation, commands });
  latest.current = { conversation, commands };

  const items: QuickPickItem<PaletteTarget>[] = page
    ? commands.map((command) => {
        const Icon = SESSION_COMMAND_ICONS[command.id];
        return {
          id: `session-action:${page.sessionId}:${command.id}`,
          label: command.label,
          keywords: [...command.keywords],
          icon: <Icon aria-hidden className="size-4" />,
          disabledReason: command.availability.available ? undefined : command.availability.reason,
          destructive: command.destructive,
          // The actions change with the session; Recent keeps the palette's own commands.
          rememberInHistory: false,
          value: { kind: "session-action", sessionId: page.sessionId, command: command.id },
        };
      })
    : [];

  return {
    open: page !== null,
    heading: page ? `Actions for “${page.title}”` : null,
    items,
    openActions: (opened, title, parent) => {
      rememberPaletteParent(parent);
      openPaletteChildPage({
        kind: "session-actions",
        sessionId: opened.id,
        title,
        conversation: opened,
      });
    },
    acceptAction: (commandId) => {
      const { conversation: current, commands: offered } = latest.current;
      const command = offered.find((candidate) => candidate.id === commandId);
      // Gone or greyed since the row was drawn: stay, showing what applies now.
      if (!current || !command?.availability.available) return "keepOpen";
      // A dialog opens once the palette has closed, so it keeps the focus it takes.
      if (command.opensDialog) {
        return { afterClose: () => requestSessionAction(current, commandId) };
      }
      requestSessionAction(current, commandId);
      return undefined;
    },
    back: () => leavePaletteChildPage(),
  };
}
