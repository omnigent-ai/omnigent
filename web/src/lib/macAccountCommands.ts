// Choosing which GitHub account this Mac reads a host's pull requests as. One
// definition feeds the panel's account menu and ⌘K, so their labels and
// behavior can't drift.

import { macAccountChoiceSaved } from "@/lib/desktopReadPath";
import {
  setDesktopMacAccount,
  type MacAccountChoice,
  type MacAccountIntent,
} from "@/lib/nativeBridge";

export interface MacAccountCommand {
  id: string;
  /** The menu row, e.g. `@bob` or `Automatic (gh’s active account)`. */
  label: string;
  /** The ⌘K row, naming the host. */
  paletteLabel: string;
  checked: boolean;
  intent: MacAccountIntent;
}

/** Each choice for `host`: Automatic, then every signed-in account. */
export function macAccountCommands(host: string, choice: MacAccountChoice): MacAccountCommand[] {
  const automatic = choice.intent.mode === "automatic";
  const chosen = choice.intent.mode === "manual" ? choice.intent.login.toLowerCase() : null;
  return [
    {
      id: `mac-account:${host}:automatic`,
      label: "Automatic (gh’s active account)",
      paletteLabel: `Read ${host} pull requests as gh’s active account on this Mac`,
      checked: automatic,
      intent: { mode: "automatic" },
    },
    ...choice.accounts
      .filter((account) => account.state === "success")
      .map((account) => ({
        id: `mac-account:${host}:${account.login.toLowerCase()}`,
        label: `@${account.login}`,
        paletteLabel: `Read ${host} pull requests as @${account.login} on this Mac`,
        checked: chosen === account.login.toLowerCase(),
        intent: { mode: "manual" as const, login: account.login },
      })),
  ];
}

/**
 * Save the choice; null when the shell refused. What's shown re-reads at once,
 * even when the account in use stays the same; a different account is read again.
 */
export async function chooseMacAccount(
  host: string,
  intent: MacAccountIntent,
): Promise<MacAccountChoice | null> {
  const choice = await setDesktopMacAccount(host, intent);
  if (choice) macAccountChoiceSaved(host, choice);
  return choice;
}
