// A repository group header's Open Repository on GitHub, Copy Repository Name and Copy
// Repository Link. One group can list a repository of the same name on more than one host, so
// each link names its host there, and only GitHub repositories get links: another provider's
// address isn't guessed.

import { CopyIcon, ExternalLinkIcon, LinkIcon } from "lucide-react";

import type { PullRequestListEntry } from "@/shell/PullRequestListRows";
import type { GroupAction } from "@/shell/usePullRequestListEntries";

import type { SelectionCopy } from "./selectionLinkActions";

/** A repository as its PRs' links name it: which provider and host serve it. */
export interface RepositoryTarget {
  /** Absent where the host predates providers, which means GitHub. */
  provider: string | undefined;
  host: string;
  repository: string;
}

/** The distinct repositories `entries` sit in, in list order. */
export function repositoryTargets(entries: readonly PullRequestListEntry[]): RepositoryTarget[] {
  const seen = new Map<string, RepositoryTarget>();
  for (const { association } of entries) {
    const target = {
      provider: association.provider,
      host: association.host.toLowerCase(),
      repository: association.repository,
    };
    const key = [target.provider ?? "github", target.host, target.repository.toLowerCase()].join(
      "\n",
    );
    if (!seen.has(key)) seen.set(key, target);
  }
  return [...seen.values()];
}

/** Whether GitHub serves it, on github.com or an Enterprise host. */
export const onGitHub = (target: RepositoryTarget) =>
  !target.provider || target.provider === "github";

/** The repository's page on its GitHub host, e.g. https://github.example.com/acme/app. */
export function repositoryUrl(target: RepositoryTarget): string {
  const path = target.repository.split("/").map(encodeURIComponent).join("/");
  return `https://${target.host}/${path}`;
}

export function repositoryGroupActions({
  title,
  targets,
  open,
  copy,
}: {
  title: string;
  targets: readonly RepositoryTarget[];
  open: (url: string) => void;
  copy: (copy: SelectionCopy) => void;
}): GroupAction[] {
  // Only a header that lists repositories offers them; a Status or Author header lists none.
  if (targets.length === 0) return [];
  const linked = targets.filter(onGitHub);
  // With one host the header says GitHub; with several, each link names its host.
  const where = (target: RepositoryTarget) => (linked.length > 1 ? ` on ${target.host}` : "");
  const actions: GroupAction[] = linked.map((target) => ({
    id: `open-repository:${target.host}`,
    label:
      linked.length > 1 ? `Open ${target.repository}${where(target)}` : "Open Repository on GitHub",
    paletteLabel: `Open ${target.repository} on ${linked.length > 1 ? target.host : "GitHub"}`,
    icon: ExternalLinkIcon,
    keywords: ["open", "repository", "repo", "github", "browser", target.host],
    run: () => open(repositoryUrl(target)),
  }));
  actions.push({
    id: "copy-repository-name",
    label: "Copy Repository Name",
    paletteLabel: `Copy Repository Name of ${title}`,
    icon: CopyIcon,
    keywords: ["copy", "repository", "repo", "name", "clipboard"],
    run: () =>
      copy({
        text: title,
        copied: "Repository name copied",
        failed: "Couldn’t copy the repository name",
      }),
  });
  for (const target of linked) {
    actions.push({
      id: `copy-repository-link:${target.host}`,
      label:
        linked.length > 1
          ? `Copy Link to ${target.repository}${where(target)}`
          : "Copy Repository Link",
      paletteLabel: `Copy Link to ${target.repository}${linked.length > 1 ? where(target) : " on GitHub"}`,
      icon: LinkIcon,
      keywords: ["copy", "repository", "repo", "link", "url", "clipboard", target.host],
      run: () =>
        copy({
          text: repositoryUrl(target),
          copied: "Repository link copied",
          failed: "Couldn’t copy the repository link",
        }),
    });
  }
  return actions;
}
