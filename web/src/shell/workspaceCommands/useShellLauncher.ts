// Opens a new shell in a session's workspace: the one implementation behind the
// "+" menu, the new-shell chord, and ⌘K.

import { useState } from "react";

import { useSessionAgent } from "@/hooks/useAgents";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import { type TerminalInfo, terminalTabKey, useCreateTerminal } from "@/hooks/useTerminals";

import { resolveDefaultShell, readPreferredShell, writePreferredShell } from "../preferredShell";
import { type ShellFacts, shellConnectState } from "./model";

export interface ShellLauncher {
  /** `null` when the agent declares no terminals. */
  shells: ShellFacts | null;
  /** Start `name`; `remember` makes it the default from then on, as picking another shell does. */
  launch: (name: string, options?: { remember?: boolean }) => void;
  /** Why the last launch failed, until the next one. */
  error: Error | null;
}

export function useShellLauncher(
  conversationId: string | null,
  {
    liveness,
    canEdit = true,
    onOpened,
    onCreateStart,
    onCreateError,
  }: {
    liveness?: SessionLiveness;
    /** The viewer can edit the session; viewers can't open shells. */
    canEdit?: boolean;
    /** Show the new shell's tab, by its tab key. */
    onOpened: (terminalKey: string) => void;
    /** Before the create resolves, so its tab can be focused as soon as it lists. */
    onCreateStart?: () => void;
    /** The create failed; *failed* is the shell tab opened early, if any. */
    onCreateError?: (failed?: TerminalInfo) => void;
  },
): ShellLauncher {
  const { data: agent } = useSessionAgent(conversationId);
  const create = useCreateTerminal(conversationId ?? "");
  // The remembered shell type, seeded from storage and updated as the user picks another.
  const [preferred, setPreferred] = useState<string | null>(() => readPreferredShell());
  const declared = agent?.terminals ?? [];
  const defaultShell = conversationId ? resolveDefaultShell(declared, preferred) : null;
  const shells: ShellFacts | null =
    defaultShell === null
      ? null
      : {
          declared,
          defaultShell,
          connect: shellConnectState(liveness),
          launching: create.isPending,
          canEdit,
        };
  const launch = (name: string, { remember = false }: { remember?: boolean } = {}) => {
    if (remember) {
      writePreferredShell(name);
      setPreferred(name);
    }
    onCreateStart?.();
    create.launch(name, {
      onOpen: (info) => onOpened(terminalTabKey(info)),
      onError: (_error, opened) => onCreateError?.(opened),
    });
  };
  return { shells, launch, error: create.error };
}
