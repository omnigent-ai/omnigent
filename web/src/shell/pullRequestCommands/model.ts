// The actions every surface offers on one linked pull request: the Pulls list row's
// menu and context menu, the PR details header, and ⌘K. This module decides each
// action's labels, value and availability from plain facts, so the surfaces can't
// drift. It holds no React; icons and implementations live beside the surfaces.

import { type GitProviderDisplay, gitProviderCopy } from "@/lib/gitProviders";
import { parseHttpUrl } from "@/lib/openLinkInApp";
import type { PullRequestReviewSite } from "@/lib/pullRequestIntegrations";
import { REMOVAL_PENDING_REASON } from "@/pullRequestList/removalProgress";
import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  unavailableBecause,
} from "@/store/paletteCommands";

/** Every pull request action, in the order a full menu lists them. */
export const PULL_REQUEST_COMMAND_IDS = [
  "refresh",
  "link",
  "open",
  "open-in-new-tab",
  "open-in-workspace",
  "open-on-provider",
  "open-in-review-site",
  "copy-link",
  "copy-review-site-link",
  "copy-number",
  "copy-title",
  "copy-branch",
  "remove",
] as const;

export type PullRequestCommandId = (typeof PULL_REQUEST_COMMAND_IDS)[number];

/** Menus put a separator between sections. */
export type PullRequestCommandSection = "refresh" | "link" | "open" | "copy" | "remove";

/** The pull request the actions are for. */
export interface PullRequestTarget {
  url: string;
  /** The exact page a link names, such as its Files tab, which opening on the site and copying keep. */
  linkUrl?: string;
  number: number;
  /** Its real title; `null` while it loads or after its first refresh failed. */
  title: string | null;
  /** Its head branch's name; `null` or absent until the PR has been read. */
  headRef?: string | null;
  provider?: string | null;
  providerDisplay?: GitProviderDisplay | null;
}

export interface PullRequestCommandFacts {
  /** The surface can show the PR's details, and isn't already showing them. */
  openable: boolean;
  /** The review site whose links show here, or null where none does. */
  reviewSite: PullRequestReviewSite | null;
  /** Whether it can be removed from the session: hidden without edit access or tracking. */
  removal: "hidden" | "pending" | "available";
  /** Why it can't be removed while `removal` is pending, saying how the removal under way is going. */
  removalPendingReason?: string;
  /**
   * Whether it can be linked to the session, for a surface showing a PR the session
   * may not have yet, such as a link in chat; hidden when absent.
   */
  link?: CommandAvailability;
  /** This surface's tabs sidebar can show the PR in its own tab; hidden elsewhere. */
  workspaceTab?: boolean;
  /** The surface opens PRs in tabs, so a PR can open in a new one behind it; hidden elsewhere. */
  newTab?: boolean;
  /**
   * Whether this one PR can be read again here: offered by the PR's own details only, and
   * unavailable while a refresh of it runs or it can't be read where it's read. Hidden when absent.
   */
  refresh?: CommandAvailability;
}

export const REFRESHING_PULL_REQUEST_REASON = "Refreshing this pull request…";
/** Where the pull request is read, it can't be now; the details say how to fix that. */
export const REFRESH_BLOCKED_REASON = "This pull request can’t be read where it’s read now.";

export { REMOVAL_PENDING_REASON };

export interface PullRequestCommand {
  id: PullRequestCommandId;
  section: PullRequestCommandSection;
  /** The label in the PR's own menu, where the PR is already clear. */
  menuLabel: string;
  /** The label in ⌘K, which names the PR. */
  paletteLabel: string;
  /** Its label inside an Open › or Copy › submenu, where that verb is already said. */
  nestedLabel?: string;
  /** Its label as a button of its own beside the PR, such as on its hover card. */
  buttonLabel?: string;
  keywords: readonly string[];
  availability: CommandAvailability;
  /** An external page it opens. */
  href?: string;
  /** It shows the PR's details, which take focus once the menu or ⌘K has closed. */
  movesFocus?: boolean;
  /** What it copies, and the toasts that confirm it. */
  copy?: { value: string; copied: string; failed: string };
}

/** How ⌘K names the PR, e.g. "#12 Fix the build". */
export function pullRequestPaletteName(target: PullRequestTarget): string {
  const copy = gitProviderCopy(target.provider, undefined, target.providerDisplay);
  const reference = `${copy.prNumberPrefix}${target.number}`;
  return target.title ? `${reference} ${target.title}` : reference;
}

export function pullRequestCommands(
  target: PullRequestTarget,
  facts: PullRequestCommandFacts,
): PullRequestCommand[] {
  const provider = gitProviderCopy(target.provider, undefined, target.providerDisplay);
  const named = pullRequestPaletteName(target);
  const request = provider.requestName;
  const linkUrl = target.linkUrl ?? target.url;
  const href = parseHttpUrl(linkUrl)?.href;
  // Eligibility comes from the URL itself, even where the site's links show.
  const site = facts.reviewSite;
  const review = site?.reviewUrl(target.url) ?? null;
  const siteName = site?.name ?? "";
  const siteKeywords = siteName.toLowerCase().split(/\s+/).filter(Boolean);
  const keywords = ["pull request", "pr", request];
  return [
    {
      id: "refresh",
      section: "refresh",
      menuLabel: "Refresh Pull Request",
      paletteLabel: `Refresh ${named}`,
      keywords: [...keywords, "reload", "update", "sync", "refresh"],
      availability: facts.refresh ?? HIDDEN,
    },
    {
      id: "link",
      section: "link",
      menuLabel: "Link to this session",
      paletteLabel: `Link ${named} to this session`,
      keywords: [...keywords, "link", "attach", "add", "session"],
      availability: facts.link ?? HIDDEN,
    },
    {
      id: "open",
      section: "open",
      menuLabel: "Open",
      paletteLabel: `Open ${named}`,
      keywords: [...keywords, "details", "show"],
      availability: facts.openable ? AVAILABLE : HIDDEN,
      movesFocus: true,
    },
    {
      id: "open-in-new-tab",
      section: "open",
      // Top level in the row menus, above the Open › submenu.
      menuLabel: "Open in new tab",
      paletteLabel: `Open ${named} in a new tab`,
      keywords: [...keywords, "tab", "new", "background"],
      availability: facts.newTab ? AVAILABLE : HIDDEN,
    },
    {
      id: "open-in-workspace",
      section: "open",
      menuLabel: "Open in workspace sidebar",
      nestedLabel: "In workspace sidebar",
      buttonLabel: "Open in Sidebar",
      paletteLabel: `Open ${named} in the workspace sidebar`,
      keywords: [...keywords, "workspace", "sidebar", "tab", "show"],
      availability: facts.workspaceTab ? AVAILABLE : HIDDEN,
      movesFocus: true,
    },
    {
      id: "open-on-provider",
      section: "open",
      menuLabel: `Open on ${provider.label}`,
      nestedLabel: `On ${provider.label}`,
      paletteLabel: `Open ${named} on ${provider.label}`,
      keywords: [...keywords, "browser", "web"],
      availability: href ? AVAILABLE : HIDDEN,
      href,
    },
    {
      id: "open-in-review-site",
      section: "open",
      menuLabel: `Open in ${siteName}`,
      nestedLabel: `In ${siteName}`,
      paletteLabel: `Open ${named} in ${siteName}`,
      keywords: [...keywords, ...siteKeywords, "browser"],
      availability: review ? AVAILABLE : HIDDEN,
      href: review ?? undefined,
    },
    {
      id: "copy-link",
      section: "copy",
      menuLabel: `Copy ${provider.label} link`,
      nestedLabel: `${provider.label} link`,
      paletteLabel: `Copy ${provider.label} link to ${named}`,
      keywords: [...keywords, "url", "clipboard"],
      availability: AVAILABLE,
      copy: {
        value: linkUrl,
        copied: `${provider.label} link copied`,
        failed: `Couldn’t copy the ${provider.label} link`,
      },
    },
    {
      id: "copy-review-site-link",
      section: "copy",
      menuLabel: `Copy ${siteName} link`,
      nestedLabel: `${siteName} link`,
      paletteLabel: `Copy ${siteName} link to ${named}`,
      keywords: [...keywords, ...siteKeywords, "url", "clipboard"],
      availability: review ? AVAILABLE : HIDDEN,
      copy: review
        ? {
            value: review,
            copied: `${siteName} link copied`,
            failed: `Couldn’t copy the ${siteName} link`,
          }
        : undefined,
    },
    {
      id: "copy-number",
      section: "copy",
      menuLabel: `Copy ${request} number`,
      nestedLabel: `${capitalized(request)} number`,
      paletteLabel: `Copy ${request} number of ${named}`,
      keywords: [...keywords, "id", "clipboard"],
      availability: AVAILABLE,
      copy: {
        value: String(target.number),
        copied: `${capitalized(request)} number copied`,
        failed: `Couldn’t copy the ${request} number`,
      },
    },
    {
      id: "copy-title",
      section: "copy",
      menuLabel: "Copy title",
      nestedLabel: "Title",
      paletteLabel: `Copy title of ${named}`,
      keywords: [...keywords, "name", "clipboard"],
      // A row still loading only knows a placeholder title.
      availability: target.title ? AVAILABLE : HIDDEN,
      copy: target.title
        ? { value: target.title, copied: "Title copied", failed: "Couldn’t copy the title" }
        : undefined,
    },
    {
      id: "copy-branch",
      section: "copy",
      menuLabel: "Copy branch name",
      nestedLabel: "Branch name",
      paletteLabel: `Copy branch name of ${named}`,
      keywords: [...keywords, "branch", "head", "ref", "checkout", "clipboard"],
      // Known once the PR has been read.
      availability: target.headRef ? AVAILABLE : HIDDEN,
      copy: target.headRef
        ? {
            value: target.headRef,
            copied: "Branch name copied",
            failed: "Couldn’t copy the branch name",
          }
        : undefined,
    },
    {
      id: "remove",
      section: "remove",
      // It unlinks the PR from this session; the PR itself is untouched.
      menuLabel: "Remove from this session",
      paletteLabel: `Remove ${named} from this session`,
      keywords: [...keywords, "unlink", "detach"],
      availability:
        facts.removal === "hidden"
          ? HIDDEN
          : facts.removal === "pending"
            ? unavailableBecause(facts.removalPendingReason ?? REMOVAL_PENDING_REASON)
            : AVAILABLE,
    },
  ];
}

export function pullRequestCommandsById(
  target: PullRequestTarget,
  facts: PullRequestCommandFacts,
): Record<PullRequestCommandId, PullRequestCommand> {
  return Object.fromEntries(
    pullRequestCommands(target, facts).map((command) => [command.id, command]),
  ) as Record<PullRequestCommandId, PullRequestCommand>;
}

/** The session-wide removal of its merged and closed pull requests, as every surface offers it. */
export interface RemoveFinishedPullRequestsCommand {
  id: "remove-finished";
  /** The label in a menu that already names the session's pull requests. */
  menuLabel: string;
  /** The label in ⌘K, with how many it removes. */
  paletteLabel: string;
  keywords: readonly string[];
  availability: CommandAvailability;
}

/**
 * Remove the session's merged and closed pull requests: hidden without removal access, and
 * unavailable with none to remove or while a removal is still being made.
 */
export function removeFinishedPullRequestsCommand({
  finished,
  removal,
  removalPendingReason = REMOVAL_PENDING_REASON,
}: {
  /** How many of the session's linked pull requests are merged or closed. */
  finished: number;
  removal: PullRequestCommandFacts["removal"];
  removalPendingReason?: string;
}): RemoveFinishedPullRequestsCommand {
  return {
    id: "remove-finished",
    menuLabel: "Remove Closed Pull Requests",
    paletteLabel: `Remove merged and closed pull requests (${finished})`,
    keywords: ["remove", "unlink", "clean up", "closed", "merged", "pull requests", "pr"],
    availability:
      removal === "hidden"
        ? HIDDEN
        : removal === "pending"
          ? unavailableBecause(removalPendingReason)
          : finished === 0
            ? unavailableBecause("No merged or closed pull requests to remove.")
            : AVAILABLE,
  };
}

function capitalized(text: string): string {
  return text ? `${text[0].toUpperCase()}${text.slice(1)}` : text;
}
