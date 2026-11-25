// The actions for the pull request whose details are on screen: in ⌘K, and the details
// header's own controls and menu, from the same definitions as the list row menus.

import { useMemo, useRef, useId } from "react";

import { useEnabledReviewSite } from "@/hooks/usePullRequestReviewSite";
import {
  type CommandAvailability,
  isCommandListed,
  type PaletteCommand,
  usePaletteCommands,
} from "@/store/paletteCommands";

import { PULL_REQUEST_COMMAND_ICONS } from "./icons";
import {
  type PullRequestCommand,
  type PullRequestCommandFacts,
  type PullRequestCommandId,
  pullRequestCommands,
  type PullRequestTarget,
} from "./model";
import { runPullRequestCommand } from "./runPullRequestCommand";

const NO_COMMANDS: PaletteCommand[] = [];

export interface PullRequestInViewCommand {
  command: PullRequestCommand;
  run: () => void;
}

/**
 * Offer the PR in view's actions under "Pull request" while the details show, and
 * return the ones its header shows itself: refresh and remove. Each command keeps the session and PR it
 * was made for, so a palette opened before a switch can't act on another one.
 */
export function usePullRequestInViewCommands({
  conversationId,
  active,
  target,
  removal,
  removalPendingReason,
  onRemove,
  refresh,
  link,
  only,
}: {
  conversationId: string;
  /** False while the panel is hidden or showing the list. */
  active: boolean;
  target: PullRequestTarget | null;
  removal: PullRequestCommandFacts["removal"];
  removalPendingReason?: string;
  onRemove: (url: string) => void;
  /** Reading this PR again, where its details offer it; absent, there's no refresh. */
  refresh?: { run: () => void; availability: CommandAvailability };
  /** Linking it to the session, where it shows a PR the session doesn't link; hidden when absent. */
  link?: { run: () => void; availability: CommandAvailability };
  /** Offer only these, where the surface offers the PR's other actions itself. */
  only?: readonly PullRequestCommandId[];
}): {
  /** Every command offered for it, in the shared order. */
  all: readonly PullRequestInViewCommand[];
  remove: PullRequestInViewCommand | null;
  refresh: PullRequestInViewCommand | null;
  link: PullRequestInViewCommand | null;
} {
  const reviewSite = useEnabledReviewSite();
  // The panel makes a new handler each render; reading it late keeps the commands stable.
  const latestRemove = useRef(onRemove);
  latestRemove.current = onRemove;
  const latestRefresh = useRef(refresh?.run);
  latestRefresh.current = refresh?.run;
  const refreshAvailability = refresh?.availability;
  const latestLink = useRef(link?.run);
  latestLink.current = link?.run;
  const linkAvailability = link?.availability;
  const onlyKey = only?.join(",") ?? null;
  const { url, number, title, headRef, provider, providerDisplay } = target ?? {};
  const commands = useMemo(() => {
    if (url === undefined || number === undefined) return [];
    const current: PullRequestTarget = {
      url,
      number,
      title: title ?? null,
      headRef,
      provider,
      providerDisplay,
    };
    const handlers = {
      conversationId,
      remove: () => latestRemove.current(url),
      refresh: () => latestRefresh.current?.(),
      link: () => latestLink.current?.(),
    };
    // The details are already open, so "Open" has nothing to do.
    return pullRequestCommands(current, {
      openable: false,
      reviewSite,
      removal,
      removalPendingReason,
      refresh: refreshAvailability,
      link: linkAvailability,
    })
      .filter((command) => isCommandListed(command.availability))
      .filter((command) => !onlyKey || onlyKey.split(",").includes(command.id))
      .map((command) => ({ command, run: () => runPullRequestCommand(command, handlers) }));
  }, [
    conversationId,
    url,
    number,
    title,
    headRef,
    provider,
    providerDisplay,
    reviewSite,
    removal,
    removalPendingReason,
    refreshAvailability,
    linkAvailability,
    onlyKey,
  ]);
  const paletteCommands = useMemo<PaletteCommand[]>(
    () =>
      commands.map(({ command, run }) => ({
        id: `${command.id}:${url}`,
        label: command.paletteLabel,
        icon: PULL_REQUEST_COMMAND_ICONS[command.id],
        keywords: [...command.keywords],
        availability: command.availability,
        // The label carries the PR's title, whose words shouldn't push sessions down.
        leadsSources: false,
        // A command that shows something closes ⌘K first, so focus can land there.
        ...(command.movesFocus ? { movesFocus: true } : {}),
        run,
      })),
    [commands, url],
  );
  const paletteInstance = useId();
  usePaletteCommands(
    `pull-request:${conversationId}:${url ?? "none"}:${paletteInstance}`,
    "Pull request",
    active ? paletteCommands : NO_COMMANDS,
  );
  return {
    all: commands,
    remove: commands.find(({ command }) => command.id === "remove") ?? null,
    refresh: commands.find(({ command }) => command.id === "refresh") ?? null,
    link: commands.find(({ command }) => command.id === "link") ?? null,
  };
}
