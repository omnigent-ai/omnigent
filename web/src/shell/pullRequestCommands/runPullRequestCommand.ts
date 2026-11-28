// What a pull request action does, the same from its menus, the details header and ⌘K.

import { toast } from "sonner";

import { copyText } from "@/lib/clipboard";
import { openLink } from "@/lib/openLinkInApp";
import { isCommandAvailable } from "@/store/paletteCommands";

import type { PullRequestCommand } from "./model";

export interface PullRequestCommandHandlers {
  /** The session the PR is linked to; in-app links open in its browser. */
  conversationId: string;
  /** Show the PR's details. */
  open?: () => void;
  /** Unlink the PR from the session. */
  remove?: () => void;
  /** Link the PR to the session. */
  link?: () => void;
  /** Show the PR in its own tab of the workspace sidebar. */
  openInWorkspace?: () => void;
  /** Open the PR in a new tab behind the current one. */
  openInNewTab?: () => void;
  /** Read the PR again, only it. */
  refresh?: () => void;
}

/** Run `command`; an unavailable one does nothing. */
export function runPullRequestCommand(
  command: PullRequestCommand,
  {
    conversationId,
    open,
    remove,
    link,
    openInWorkspace,
    openInNewTab,
    refresh,
  }: PullRequestCommandHandlers,
): void {
  if (!isCommandAvailable(command.availability)) return;
  if (command.copy) {
    const { value, copied, failed } = command.copy;
    void copyText(value).then(
      () => toast.success(copied),
      () => toast.error(failed),
    );
    return;
  }
  if (command.href) {
    openLink(conversationId, command.href);
    return;
  }
  if (command.id === "open") open?.();
  else if (command.id === "remove") remove?.();
  else if (command.id === "link") link?.();
  else if (command.id === "open-in-workspace") openInWorkspace?.();
  else if (command.id === "open-in-new-tab") openInNewTab?.();
  else if (command.id === "refresh") refresh?.();
}
