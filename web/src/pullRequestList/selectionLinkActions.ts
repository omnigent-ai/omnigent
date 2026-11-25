// What a selection of pull requests offers to open and copy: each PR in the workspace sidebar,
// and its own Open on GitHub, links and branch name, read from the same commands its row offers,
// so their labels and values can't drift. The caller supplies what opening and copying do. The
// selection bar lists them in its More menu, so the bar keeps room for what changes the PRs.

import { CopyIcon, ExternalLinkIcon, GitBranchIcon, LinkIcon, PanelRightIcon } from "lucide-react";

import { gitProviderCopy } from "@/lib/gitProviders";
import type { PullRequestReviewSite } from "@/lib/pullRequestIntegrations";
import { isCommandAvailable } from "@/store/paletteCommands";
import { pullRequestCommandsById } from "@/shell/pullRequestCommands/model";
import { type PullRequestListEntry, rowCommandTarget } from "@/shell/PullRequestListRows";

import type { ListSelectionAction } from "./listSelectionActions";

/** Opening more than this many asks first. */
export const OPEN_WITHOUT_ASKING = 10;
/** The most the workspace sidebar opens at once, each as its own tab. */
export const OPEN_IN_SIDEBAR_AT_MOST = 20;
export const TOO_MANY_FOR_SIDEBAR_REASON = `Select ${OPEN_IN_SIDEBAR_AT_MOST} or fewer to open them in the sidebar.`;

export const NO_BROWSER_LINKS_REASON = "None of these can be opened in a browser.";
export const NO_BRANCHES_REASON = "Their branches aren’t known yet.";

/** One selected PR's page to open. */
export interface SelectionLink {
  url: string;
  href: string;
}

export interface SelectionCopy {
  text: string;
  copied: string;
  failed: string;
}

export interface SelectionLinkEffects {
  /** The review site whose links show here, or null where none does. */
  reviewSite: PullRequestReviewSite | null;
  /** Open `links` now, within the click that asked. */
  open: (links: readonly SelectionLink[]) => void;
  /** Ask before opening `links`, which are more than {@link OPEN_WITHOUT_ASKING}. */
  confirmOpen: (links: readonly SelectionLink[], label: string) => void;
  copy: (copy: SelectionCopy) => void;
  /** Open each of `urls` in the workspace sidebar, in order; absent where the list can't. */
  openInWorkspace?: (urls: readonly string[]) => void;
}

const plural = (count: number, one: string) => `${count} ${one}${count === 1 ? "" : "s"}`;
const ofSelection = (label: string, acting: number, selected: number) =>
  acting < selected ? `${label} (${acting} of ${selected})` : label;

export function selectionLinkActions(
  selected: readonly PullRequestListEntry[],
  effects: SelectionLinkEffects,
): ListSelectionAction[] {
  const seen = new Set<string>();
  const entries = selected.filter((entry) => {
    const fresh = !seen.has(entry.association.url);
    seen.add(entry.association.url);
    return fresh;
  });
  const total = entries.length;
  const commands = entries.map((entry) =>
    pullRequestCommandsById(rowCommandTarget(entry), {
      openable: false,
      reviewSite: effects.reviewSite,
      removal: "hidden",
    }),
  );
  const links = commands.flatMap((byId, index) => {
    const href = byId["open-on-provider"].href;
    return href && isCommandAvailable(byId["open-on-provider"].availability)
      ? [{ url: entries[index]!.association.url, href }]
      : [];
  });
  const copiesOf = (id: "copy-link" | "copy-review-site-link" | "copy-branch") =>
    commands.flatMap((byId) =>
      byId[id].copy && isCommandAvailable(byId[id].availability) ? [byId[id].copy.value] : [],
    );
  const linkCopies = copiesOf("copy-link");
  const reviewCopies = copiesOf("copy-review-site-link");
  const branchCopies = copiesOf("copy-branch");

  // One provider names where they open; a mix of them opens in the browser.
  const providers = new Set(
    entries.map(
      (entry) =>
        gitProviderCopy(entry.association.provider, undefined, entry.association.provider_display)
          .label,
    ),
  );
  const provider = providers.size === 1 ? [...providers][0]! : null;
  const where = provider ? `on ${provider}` : "in Browser";
  const asks = links.length > OPEN_WITHOUT_ASKING;
  const openLabel = `Open ${where}${asks ? "…" : ""}`;
  const pullRequests = (count: number) => plural(count || total, "Pull Request");
  const actions: ListSelectionAction[] = [];
  const { openInWorkspace } = effects;
  if (openInWorkspace) {
    const urls = entries.map((entry) => entry.association.url);
    actions.push({
      id: "open-in-workspace",
      label: "Open in Workspace Sidebar",
      paletteLabel: `Open ${pullRequests(total)} in Workspace Sidebar`,
      icon: PanelRightIcon,
      keywords: ["open", "workspace", "sidebar", "tabs", "bulk", "pull requests"],
      inMoreMenu: true,
      menuSection: "open",
      nestedLabel: "In Workspace Sidebar",
      movesFocus: true,
      unavailableReason: total > OPEN_IN_SIDEBAR_AT_MOST ? TOO_MANY_FOR_SIDEBAR_REASON : undefined,
      run: () => openInWorkspace(urls),
    });
  }
  actions.push(
    {
      id: "open-on-provider",
      label: ofSelection(openLabel, links.length, total),
      paletteLabel: `Open ${pullRequests(links.length)} ${where}${asks ? "…" : ""}`,
      icon: ExternalLinkIcon,
      keywords: ["open", "browser", "web", "bulk", "pull requests"],
      inMoreMenu: true,
      menuSection: "open",
      nestedLabel: ofSelection(
        `${provider ? "On" : "In"} ${provider ?? "Browser"}${asks ? "…" : ""}`,
        links.length,
        total,
      ),
      movesFocus: asks,
      unavailableReason: links.length ? undefined : NO_BROWSER_LINKS_REASON,
      run: () =>
        asks
          ? effects.confirmOpen(links, `Open ${plural(links.length, "pull request")} ${where}?`)
          : effects.open(links),
    },
    {
      id: "copy-links",
      label: ofSelection("Copy Links", linkCopies.length, total),
      paletteLabel: `Copy Links to ${pullRequests(linkCopies.length)}`,
      icon: LinkIcon,
      keywords: ["copy", "url", "clipboard", "bulk", "pull requests"],
      inMoreMenu: true,
      menuSection: "copy",
      nestedLabel: ofSelection("Links", linkCopies.length, total),
      run: () =>
        effects.copy({
          text: linkCopies.join("\n"),
          copied: `Copied ${plural(linkCopies.length, "link")}`,
          failed: "Couldn’t copy the links",
        }),
    },
  );
  const siteName = effects.reviewSite?.name;
  if (siteName && reviewCopies.length > 0) {
    actions.push({
      id: "copy-review-site-links",
      label: ofSelection(`Copy ${siteName} Links`, reviewCopies.length, total),
      paletteLabel: `Copy ${siteName} Links to ${pullRequests(reviewCopies.length)}`,
      icon: CopyIcon,
      keywords: ["copy", ...siteName.toLowerCase().split(/\s+/), "url", "clipboard", "bulk"],
      inMoreMenu: true,
      menuSection: "copy",
      nestedLabel: ofSelection(`${siteName} Links`, reviewCopies.length, total),
      run: () =>
        effects.copy({
          text: reviewCopies.join("\n"),
          copied: `Copied ${plural(reviewCopies.length, `${siteName} link`)}`,
          failed: `Couldn’t copy the ${siteName} links`,
        }),
    });
  }
  actions.push({
    id: "copy-branch-names",
    label: ofSelection("Copy Branch Names", branchCopies.length, total),
    paletteLabel: `Copy Branch Names of ${pullRequests(branchCopies.length)}`,
    icon: GitBranchIcon,
    keywords: ["copy", "branch", "head", "ref", "checkout", "clipboard", "bulk"],
    inMoreMenu: true,
    menuSection: "copy",
    nestedLabel: ofSelection("Branch Names", branchCopies.length, total),
    unavailableReason: branchCopies.length ? undefined : NO_BRANCHES_REASON,
    run: () =>
      effects.copy({
        text: branchCopies.join("\n"),
        copied: `Copied ${plural(branchCopies.length, "branch name")}`,
        failed: "Couldn’t copy the branch names",
      }),
  });
  return actions;
}
