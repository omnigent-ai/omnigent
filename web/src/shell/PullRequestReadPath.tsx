// Where the panel's pull request was read: on this Mac, as which account, or by
// the session's machine. On the Mac it doubles as the account menu, whose
// choices also show in ⌘K from the same definitions. On the Pull Requests page,
// whose header picks the account, it only names it.

import { useMemo, useState, useId } from "react";
import { ChevronDownIcon, UserRoundIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { PullRequestMacRead } from "@/newPullRequestPage/hooks/usePullRequests";
import { useMacAccountChoice } from "@/hooks/useMacAccounts";
import {
  readOnSessionMachine,
  readOnThisMacAgain,
  readsOnSessionMachine,
  useDesktopReadPathVersion,
} from "@/lib/desktopReadPath";
import { chooseMacAccount, macAccountCommands } from "@/lib/macAccountCommands";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

/** The label for a PR read on this Mac. */
export function macReadLabel(read: PullRequestMacRead): string {
  if (read.provider === "gitspoke") return "Read on this Mac with your certificate";
  return read.login ? `Read as @${read.login} on this Mac` : "Read on this Mac";
}

export function PullRequestReadPath({
  active = true,
  read,
  prUrl,
  hostCanAnswer,
}: {
  /** False while its panel is mounted but hidden: its ⌘K rows belong to the shown panel. */
  active?: boolean;
  /** Set when this Mac read the PR; absent when the session's machine did. */
  read: PullRequestMacRead | undefined;
  prUrl: string;
  /** The session's machine is reachable, so it can read the PR instead. */
  hostCanAnswer: boolean;
}) {
  const host = read?.provider === "github" ? read.host : null;
  const choice = useMacAccountChoice(host);
  const [failed, setFailed] = useState(false);
  const commands = useMemo(
    () => (host && choice ? macAccountCommands(host, choice) : []),
    [host, choice],
  );
  const choose = (intent: (typeof commands)[number]["intent"]) => {
    if (!host) return;
    setFailed(false);
    void chooseMacAccount(host, intent).then((saved) => setFailed(saved === null));
  };
  const paletteInstance = useId();
  usePaletteCommands(
    `pull-request-mac-account:${paletteInstance}`,
    "Pull requests",
    useMemo<PaletteCommand[]>(
      () =>
        (active ? commands : [])
          .filter((command) => !command.checked)
          .map((command) => ({
            id: command.id,
            label: command.paletteLabel,
            icon: UserRoundIcon,
            keywords: ["github", "account", "login", "mac", "pull request", host ?? ""],
            run: () => choose(command.intent),
          })),
      // `choose` reads only `host`, which `commands` already depends on.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      [commands, active],
    ),
  );

  useDesktopReadPathVersion();
  if (!read) {
    if (!readsOnSessionMachine(prUrl)) return null;
    return (
      <p className="flex items-center gap-2 text-xs text-muted-foreground">
        Read with the session’s machine
        <Button
          variant="link"
          className="h-auto p-0 text-xs"
          onClick={() => readOnThisMacAgain(prUrl)}
        >
          Read on this Mac
        </Button>
      </p>
    );
  }
  const label = macReadLabel(read);
  if (!host) return <p className="text-xs text-muted-foreground">{label}</p>;
  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <DropdownMenu>
        <DropdownMenuTrigger
          className="flex items-center gap-0.5 rounded-sm hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          aria-label={`${label}. Change account`}
        >
          {label}
          <ChevronDownIcon aria-hidden className="size-3" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          <DropdownMenuLabel>{`GitHub account for ${host}`}</DropdownMenuLabel>
          <DropdownMenuRadioGroup
            value={commands.find((command) => command.checked)?.id ?? ""}
            onValueChange={(id) => {
              const command = commands.find((candidate) => candidate.id === id);
              if (command && !command.checked) choose(command.intent);
            }}
          >
            {commands.map((command) => (
              <DropdownMenuRadioItem key={command.id} value={command.id}>
                {command.label}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
          {hostCanAnswer && (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={() => readOnSessionMachine(prUrl)}>
                Read with the session’s machine instead
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      {failed && (
        <span role="status" className="text-destructive">
          Couldn’t change the account.
        </span>
      )}
    </div>
  );
}
