// The header's top-right "where this PR is read" trigger, and its three-item menu. On the Pull
// Requests page, a PR read as the page's own account shows none: the page's header names it.

import { ChevronDownIcon, LaptopIcon, ServerIcon } from "lucide-react";

import {
  DESCRIBED_MENU_CONTENT_CLASS,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useMacWriteRoute } from "@/newPullRequestPage/hooks/usePullRequests";
import { useShowsMacAccount, type VisibleMacAccount } from "@/shell/MacWriteNotices";
import type { ReadChoice } from "@/lib/pullRequestReadRoute";
import { cn } from "@/lib/utils";

import {
  READ_SOURCE_MENU_HEADING,
  readSourceChangesNote,
  type ReadSourceChoice,
  readSourceChoices,
  type ReadSourceTriggerText,
  readSourceTriggerText,
} from "./pullRequestSource";
import { REPO_ACCOUNT_HEADING, repoAccountNote } from "./repoAccountChoices";
import { useReadSource } from "./useReadSource";
import { type RepoAccountView, useRepoAccount } from "./useRepoAccount";

export interface ReadSourceTriggerView {
  text: ReadSourceTriggerText;
  /** This Mac was chosen but can't read the PR. */
  blocked: boolean;
  /** Read on the session's machine rather than this Mac. */
  onHost: boolean;
  choices: ReadSourceChoice[];
  choose: (choice: ReadChoice) => void;
  /** Where changes to the PR go, which may differ from where it's read. */
  changesNote: string;
  /** The account the trigger names, where it names one: this Mac's for the PR's host. */
  account: VisibleMacAccount | null;
  /** Which of this Mac's accounts the PR's repository is read as; null where none is offered. */
  repoAccount: RepoAccountView | null;
  /** The trigger's accessible name for its place, e.g. a blocked Mac and why. */
  blockedName: string;
}

/**
 * The trigger's view, or null where there's nothing to choose: a GitSpoke PR reads only on
 * this Mac, and a PR this Mac can't read for its host has no other place to offer.
 */
export function useReadSourceTrigger(prUrl: string): ReadSourceTriggerView | null {
  const source = useReadSource(prUrl);
  const writeRoute = useMacWriteRoute(prUrl);
  const repoAccount = useRepoAccount(prUrl);
  if (!source.offered) return null;
  const repoBlocked = source.route.kind === "blocked" && source.route.reason === "repo_account";
  return {
    text: readSourceTriggerText(source.route, source.login),
    blocked: source.route.kind === "blocked",
    onHost: source.route.kind === "host",
    choices: readSourceChoices(source),
    choose: source.choose,
    changesNote: readSourceChangesNote(writeRoute?.actor.login ?? null),
    account:
      source.route.kind === "mac" && source.host && source.login
        ? { host: source.host, login: source.login }
        : null,
    repoAccount,
    blockedName:
      repoBlocked && source.login
        ? `This Mac as @${source.login}, which can’t be used now`
        : "This Mac, which can’t read it",
  };
}

/** The trigger's box, shared with the header's measuring copies so they size alike. */
export const READ_SOURCE_TRIGGER_CLASS =
  "flex h-6 max-w-full min-w-0 items-center gap-1 rounded-md px-1.5 text-ui whitespace-nowrap";

/** How much of the trigger's text shows: the account truncates first, then the place goes. */
export type ReadSourceTriggerParts = "place-and-account" | "place" | "icon";

/** What the trigger shows: an icon for the place, the place, then the account. */
export function ReadSourceTriggerFace({
  view,
  parts = "place-and-account",
}: {
  view: ReadSourceTriggerView;
  parts?: ReadSourceTriggerParts;
}) {
  const Icon = view.onHost ? ServerIcon : LaptopIcon;
  return (
    <>
      <Icon aria-hidden className="size-3.5 shrink-0" />
      {parts !== "icon" && <span className="shrink-0">{view.text.place}</span>}
      {parts === "place-and-account" && view.text.account && (
        // The leading space keeps the text whole when copied; the gap does the spacing.
        <span className="min-w-0 truncate">{` · ${view.text.account}`}</span>
      )}
      <ChevronDownIcon aria-hidden className="size-3 shrink-0" />
    </>
  );
}

export function ReadSourceTrigger({
  view,
  parts,
}: {
  view: ReadSourceTriggerView;
  parts?: ReadSourceTriggerParts;
}) {
  const name = view.blocked ? view.blockedName : view.text.full;
  // Only this trigger, as shown, says the page names the account; never the header's measuring
  // copies of its face, and not while the header has room for its icon alone.
  useShowsMacAccount(parts !== "icon" && view.text.account ? view.account : null);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label={`Read from ${name}. Change`}
        // The full account stays in the tooltip when the header shortens it.
        title={`Read from ${name}`}
        className={cn(
          READ_SOURCE_TRIGGER_CLASS,
          "hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none data-[state=open]:bg-muted data-[state=open]:text-foreground",
          view.blocked ? "text-warning" : "text-muted-foreground",
        )}
      >
        <ReadSourceTriggerFace view={view} parts={parts} />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className={DESCRIBED_MENU_CONTENT_CLASS}>
        <DropdownMenuLabel className="text-xs font-normal whitespace-normal">
          {READ_SOURCE_MENU_HEADING}
        </DropdownMenuLabel>
        <DropdownMenuRadioGroup
          value={view.choices.find((choice) => choice.checked)?.choice}
          onValueChange={(value) => view.choose(value as ReadChoice)}
        >
          {view.choices.map((choice) => (
            <DropdownMenuRadioItem
              key={choice.choice}
              value={choice.choice}
              description={choice.description}
            >
              {choice.label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
        {view.repoAccount && <RepoAccountGroup view={view.repoAccount} />}
        <DropdownMenuSeparator />
        <p className="px-1.5 py-1 text-xs whitespace-normal text-muted-foreground">
          {view.changesNote}
        </p>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** The menu's account group: Automatic, then each of this Mac's accounts, kept per repository. */
function RepoAccountGroup({ view }: { view: RepoAccountView }) {
  const checked = view.choices.find((choice) => choice.checked);
  return (
    <>
      <DropdownMenuSeparator />
      <DropdownMenuLabel className="text-xs font-normal whitespace-normal">
        {REPO_ACCOUNT_HEADING}
      </DropdownMenuLabel>
      <DropdownMenuRadioGroup
        value={checked?.id}
        onValueChange={(value) => {
          const choice = view.choices.find((candidate) => candidate.id === value);
          if (choice && !choice.checked) view.choose(choice.intent);
        }}
      >
        {view.choices.map((choice) => (
          <DropdownMenuRadioItem key={choice.id} value={choice.id} description={choice.description}>
            {choice.label}
          </DropdownMenuRadioItem>
        ))}
      </DropdownMenuRadioGroup>
      <p className="px-1.5 py-1 text-xs whitespace-normal text-muted-foreground">
        {repoAccountNote(view.repository)}
      </p>
    </>
  );
}
