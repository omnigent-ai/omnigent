// The new page's ⌘K rows for where a pull request is read and as which Mac account. The
// panel mounts it for the selected PR, so the rows exist on every tab and before the PR
// loads, and leave when the panel hides.

import { useId, useMemo } from "react";
import { LaptopIcon, ServerIcon, SparklesIcon, UserRoundIcon } from "lucide-react";

import { showToast } from "@/components/ui/toast";
import { useMacAccountChoice } from "@/hooks/useMacAccounts";
import { chooseMacAccount, macAccountCommands } from "@/lib/macAccountCommands";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

import { readSourceChoices } from "./pullRequestSource";
import { setReadSourceFailure, useReadSource } from "./useReadSource";
import { useRepoAccount } from "./useRepoAccount";

export const ACCOUNT_SWITCH_FAILED = "Couldn’t change the account.";

const CHOICE_ICONS = { automatic: SparklesIcon, mac: LaptopIcon, session: ServerIcon } as const;

/** Switch this Mac's account for `host`, saying so wherever the user is when it fails. */
export async function switchMacAccount(
  prUrl: string,
  host: string,
  intent: Parameters<typeof chooseMacAccount>[1],
): Promise<void> {
  const saved = await chooseMacAccount(host, intent);
  if (saved) {
    setReadSourceFailure(prUrl, null);
    return;
  }
  setReadSourceFailure(prUrl, ACCOUNT_SWITCH_FAILED);
  showToast(ACCOUNT_SWITCH_FAILED);
}

export function ReadSourceOwner({ prUrl, active }: { prUrl: string; active: boolean }) {
  const source = useReadSource(prUrl);
  const accountChoice = useMacAccountChoice(source.host);
  // The repository's own account choice, beside the host-wide one Automatic follows.
  const repoAccount = useRepoAccount(prUrl);
  const instance = useId();
  const commands = useMemo<PaletteCommand[]>(() => {
    if (!active) return [];
    const rows: PaletteCommand[] = [];
    if (source.offered) {
      for (const choice of readSourceChoices(source)) {
        if (choice.checked) continue;
        rows.push({
          id: `pull-request-read-source:${choice.choice}:${prUrl}`,
          label: choice.paletteLabel,
          icon: CHOICE_ICONS[choice.choice],
          keywords: ["read", "source", "mac", "session", "machine", "pull request"],
          run: () => source.choose(choice.choice),
        });
      }
    }
    const host = source.host;
    if (repoAccount) {
      for (const choice of repoAccount.choices) {
        if (choice.checked) continue;
        rows.push({
          id: `${choice.id}:${prUrl}`,
          label: choice.paletteLabel,
          icon: UserRoundIcon,
          keywords: ["github", "account", "login", "mac", "repository", "pull request"],
          run: () => repoAccount.choose(choice.intent),
        });
      }
    }
    if (host && accountChoice) {
      for (const command of macAccountCommands(host, accountChoice)) {
        if (command.checked) continue;
        rows.push({
          id: `${command.id}:${prUrl}`,
          label: command.paletteLabel,
          icon: UserRoundIcon,
          keywords: ["github", "account", "login", "mac", "pull request", host],
          run: () => void switchMacAccount(prUrl, host, command.intent),
        });
      }
    }
    return rows;
    // `source` is rebuilt each render; these are what its rows read.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    active,
    prUrl,
    source.offered,
    source.choice,
    source.automaticPick,
    source.login,
    source.host,
    accountChoice,
    repoAccount?.choices,
  ]);
  usePaletteCommands(`pull-request-read-source:${instance}`, "Pull request", commands);
  return null;
}
