// A diff review file's header actions, from the shared file commands: the same
// labels, availability, and host as every other file surface.

import type { DiffReviewFileAction } from "@/components/diff-review/diffReviewFileAction";
import { followLinkWithPopupFallback, openLink } from "@/lib/openLinkInApp";
import type { CodeHostFileLink } from "./codeHostFileLink";
import { copyWithToast } from "./copyWithToast";
import { requestFileAction } from "./fileActionStore";
import { FILE_COMMAND_ICONS } from "./icons";
import { ACTIVE_FILE_PALETTE_LABELS, type FileCommandId, fileCommands } from "./model";
import type { FileCommandTarget } from "./target";
import { type FileCommandEnvironment, fileCommandFacts } from "./useFileCommandFacts";

/** Where a pull request's files live: in a repository, not on any host the app reaches. */
export const REPOSITORY_FILE_ENVIRONMENT: FileCommandEnvironment = {
  local: false,
  sshTarget: null,
  linkable: false,
  revealSupported: false,
  fileManager: "",
};

/**
 * `target`'s header actions in `environment`. Open is offered only with
 * `onOpen`, which opens it where the review's surface shows files, the code
 * host action only with `codeHostLink`, and Copy previous path only for a rename's
 * `previousPath`. In the file viewer the review's file is the `activeFile`, whose
 * ⌘K actions take VS Code's names.
 */
export function reviewFileActions(
  target: FileCommandTarget,
  environment: FileCommandEnvironment,
  {
    onOpen = null,
    codeHostLink = null,
    previousPath = null,
    activeFile = false,
  }: {
    onOpen?: (() => void) | null;
    codeHostLink?: CodeHostFileLink | null;
    previousPath?: string | null;
    activeFile?: boolean;
  } = {},
): DiffReviewFileAction[] {
  const facts = {
    ...fileCommandFacts(target, environment, { openable: onOpen !== null }),
    codeHostLink,
    previousPath,
  };
  const conversationId = target.conversationId ?? undefined;
  // The menu's row is the link itself; ⌘K opens the same address.
  const codeHostUrl = codeHostLink?.url ?? null;
  return fileCommands(facts).flatMap((command) => {
    const { id, availability } = command;
    if (!availability.available && availability.reason === undefined) return [];
    return [
      {
        id,
        label: command.label,
        paletteLabel: activeFile ? ACTIVE_FILE_PALETTE_LABELS[id] : undefined,
        icon: FILE_COMMAND_ICONS[id],
        keywords: command.keywords,
        disabledReason: availability.available ? undefined : availability.reason,
        link:
          id === "open-on-code-host" && availability.available && codeHostUrl
            ? {
                href: codeHostUrl,
                follow: (event: MouseEvent) =>
                  followLinkWithPopupFallback(event, codeHostUrl, conversationId),
              }
            : undefined,
        run: () => runReviewFileAction(target, id, { onOpen, codeHostUrl, previousPath }),
      },
    ];
  });
}

function runReviewFileAction(
  target: FileCommandTarget,
  id: FileCommandId,
  {
    onOpen,
    codeHostUrl,
    previousPath,
  }: { onOpen: (() => void) | null; codeHostUrl: string | null; previousPath: string | null },
) {
  if (id === "open") onOpen?.();
  else if (id === "open-on-code-host") {
    if (codeHostUrl) openLink(target.conversationId ?? undefined, codeHostUrl);
  } else if (id === "copy-previous-path") {
    if (previousPath) {
      copyWithToast(previousPath, "Copied previous path", "Couldn't copy the previous path");
    }
  }
  // A review offers its own new tab, and its folder rows re-root nothing.
  else if (id === "open-in-new-tab" || id === "open-as-root") return;
  else requestFileAction(target, id);
}

/** The copy actions a review's tree offers for a folder, beside its own Collapse and Expand. */
const FOLDER_ACTION_IDS: ReadonlySet<string> = new Set(["copy-path", "copy-relative-path"]);

/**
 * A changed-files tree folder's actions in `environment`, from the same shared file commands as
 * its files': copying its path, as the folder's files copy theirs.
 */
export function reviewFolderActions(
  target: FileCommandTarget,
  environment: FileCommandEnvironment,
): DiffReviewFileAction[] {
  return reviewFileActions({ ...target, directory: true }, environment).filter((action) =>
    FOLDER_ACTION_IDS.has(action.id),
  );
}
