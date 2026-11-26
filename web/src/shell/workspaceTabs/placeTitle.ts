// What a workspace tab is called. The title is derived from the tab's place and
// live facts every time either changes, never stored, so the label always says
// what the tab shows, including after in-tab navigation such as cycling files.

import { browserPageLabel } from "@/lib/browserPageLabel";
import { SUBAGENT_TAB_FALLBACK_LABEL } from "@/shell/SubagentTab";

import type { WorkspacePlace } from "./workspacePlace";

/** Which icon the strip shows; the component maps these to its icon set. */
export type PlaceIcon =
  | "new"
  | "files"
  | "file"
  | "changes"
  | "pulls"
  | "pull"
  | "terminal"
  | "agents"
  | "tasks"
  | "agent"
  | "chat"
  | "search"
  | "web";

export interface PlaceTitle {
  label: string;
  /** The full text, for the tooltip and accessible name. */
  tooltip: string;
  icon: PlaceIcon;
  /** A count or position such as `6` or `2/6`. */
  badge?: string;
  /** File names truncate in the middle so the extension stays visible; others at the end. */
  truncate: "middle" | "end";
  /** Its resource is still loading; the strip adds a spinner but keeps the label. */
  connecting: boolean;
}

/** Live facts a title needs, gathered by the caller from the stores that own them. */
export interface PlaceTitleFacts {
  /** Paths shown by the session's other file tabs, to tell same-named files apart. */
  otherFilePaths?: readonly string[];
  workspaceRoot?: string | null;
  /** The session's changed files, in review order. */
  changedFiles?: readonly string[];
  /** How the comparison's base is described, such as "the branch point". */
  changesBaseLabel?: string;
  pullRequest?: {
    number?: number | null;
    title?: string | null;
    numberPrefix?: string;
    /** Repository, provider and (when needed) host, from `pullRequestIdentityLabels`. */
    identity?: string | null;
  };
  /** Today's shell label (`terminalDisplayLabel`) and its folder or command. */
  terminal?: { label: string; detail?: string };
  agentName?: string | null;
  chatTitle?: string | null;
  page?: { url?: string | null; title?: string | null };
  connecting?: boolean;
}

/** The Changes panel's name in the tab sidebar: diffs of work on this machine, before it's pushed. */
export const LOCAL_CHANGES_LABEL = "Diffs";

const PULL_REQUEST_VIEW_LABELS = {
  summary: "Overview",
  comments: "Comments",
  commits: "Commits",
  changes: "Changes",
} as const;

function baseName(path: string): string {
  return path.split("/").filter(Boolean).pop() ?? path;
}

function parents(path: string): string[] {
  const segments = path.split("/").filter(Boolean);
  return segments.slice(0, -1);
}

/**
 * A file's name, plus the shortest run of parent folders that tells it apart
 * from other open files with the same name: `BUILD.bazel · server`.
 */
export function distinctFileLabel(path: string, otherPaths: readonly string[] = []): string {
  const name = baseName(path);
  const rivals = otherPaths.filter((other) => other !== path && baseName(other) === name);
  if (rivals.length === 0) return name;
  const own = parents(path);
  for (let depth = 1; depth <= own.length; depth++) {
    const suffix = own.slice(-depth).join("/");
    if (!rivals.some((rival) => parents(rival).slice(-depth).join("/") === suffix)) {
      return `${name} · ${suffix}`;
    }
  }
  return path;
}

/** `text` shortened to `maxLength` characters by an ellipsis in the middle. */
export function truncateMiddle(text: string, maxLength: number): string {
  const characters = [...text];
  if (characters.length <= maxLength) return text;
  if (maxLength <= 1) return "…";
  const keep = maxLength - 1;
  const head = Math.ceil(keep / 2);
  return `${characters.slice(0, head).join("")}…${characters.slice(characters.length - (keep - head)).join("")}`;
}

export function pullRequestNumber(url: string, known?: number | null): number | null {
  if (known != null) return known;
  const match = /\/(\d+)\/?$/.exec(url.split(/[?#]/)[0]);
  return match ? Number(match[1]) : null;
}

/**
 * A pull request's repository and host read from its URL, such as
 * `ghe.example/two/b #123`, until the session's pull request facts load.
 */
function pullRequestUrlIdentity(url: string, reference: string): string {
  const parts = pullRequestUrlParts(url);
  if (!parts) return reference;
  const where = `${parts.host}${parts.repository ? `/${parts.repository}` : ""}`;
  return [where, reference].join(" ").trim();
}

/** A pull request's host and repository read from its URL, such as `ghe.example` and `two/b`. */
export function pullRequestUrlParts(url: string): { host: string; repository: string } | null {
  try {
    const parsed = new URL(url);
    const repository = parsed.pathname
      .replace(/\/(?:-\/merge_requests|pull|pullrequest)\/\d+.*$/i, "")
      .replace(/^\/+|\/+$/g, "");
    return { host: parsed.host.toLowerCase(), repository };
  } catch {
    return null;
  }
}

function cursorBadge(path: string, files: readonly string[] | undefined): string | undefined {
  if (!files) return undefined;
  const index = files.indexOf(path);
  return index === -1 ? undefined : `${index + 1}/${files.length}`;
}

export function placeTitle(place: WorkspacePlace, facts: PlaceTitleFacts = {}): PlaceTitle {
  const connecting = facts.connecting ?? false;
  const title = (
    label: string,
    icon: PlaceIcon,
    extra: Partial<Pick<PlaceTitle, "tooltip" | "badge" | "truncate">> = {},
  ): PlaceTitle => ({
    label,
    tooltip: extra.tooltip ?? label,
    icon,
    truncate: extra.truncate ?? "end",
    connecting,
    ...(extra.badge !== undefined ? { badge: extra.badge } : {}),
  });

  switch (place.kind) {
    case "new":
      return title(place.app === "terminal" ? "New shell" : "New tab", "new");
    case "files":
      if (place.path === undefined) {
        return title("Files", "files", { tooltip: facts.workspaceRoot ?? "Files" });
      }
      return title(distinctFileLabel(place.path, facts.otherFilePaths), "file", {
        tooltip: place.path,
        truncate: "middle",
      });
    case "changes": {
      const base = place.range
        ? place.range.head
          ? `${place.range.base}…${place.range.head}`
          : `${place.range.base}…working copy`
        : (facts.changesBaseLabel ?? "the branch point");
      const heading = place.range ? base : `Changes vs ${base}`;
      // Changes review shows every changed file: a path only says which file to reveal,
      // unless the tab was opened for that file on its own.
      if (place.path === undefined || (!place.range && !place.fileTab)) {
        const count = facts.changedFiles?.length;
        return title(place.range ? base : LOCAL_CHANGES_LABEL, "changes", {
          tooltip: heading,
          ...(count ? { badge: `${count}` } : {}),
        });
      }
      const badge = cursorBadge(place.path, facts.changedFiles);
      return title(baseName(place.path), "changes", {
        tooltip: `${heading} · ${place.path}`,
        truncate: "middle",
        ...(badge ? { badge } : {}),
      });
    }
    case "pulls":
      return title("Pull requests", "pulls");
    case "pull": {
      const prefix = facts.pullRequest?.numberPrefix ?? "#";
      const number = pullRequestNumber(place.url, facts.pullRequest?.number);
      const reference = number === null ? "Pull request" : `${prefix}${number}`;
      const prTitle = facts.pullRequest?.title?.trim();
      const view = PULL_REQUEST_VIEW_LABELS[place.view];
      const where = place.path
        ? `${view} · ${place.path}`
        : place.focus === "checks"
          ? "Checks"
          : view;
      const label = place.path
        ? `${reference} ${baseName(place.path)}`
        : prTitle
          ? `${reference} ${prTitle}`
          : number === null
            ? reference
            : `Pull request ${reference}`;
      const identity =
        facts.pullRequest?.identity?.trim() || pullRequestUrlIdentity(place.url, reference);
      return title(label, "pull", {
        tooltip: [identity, prTitle, where].filter(Boolean).join(" · "),
        truncate: place.path ? "middle" : "end",
      });
    }
    case "terminal": {
      const label = facts.terminal?.label ?? "Shell";
      const detail = facts.terminal?.detail;
      return title(label, "terminal", { tooltip: detail ? `${label} · ${detail}` : label });
    }
    case "agents":
      return title("Agents", "agents");
    case "tasks":
      return title("Tasks", "tasks");
    case "agent":
      return title(facts.agentName?.trim() || SUBAGENT_TAB_FALLBACK_LABEL, "agent");
    case "chat":
      if (place.id === undefined) return title("New side chat", "chat");
      return title(facts.chatTitle?.trim() || "Side chat", "chat");
    case "search":
      return place.query ? title(`Search: ${place.query}`, "search") : title("Search", "search");
    case "web": {
      // An empty or about:blank page is still the new page the tab was opened as.
      const url = facts.page?.url ?? place.url;
      if (!url || url === "about:blank") return title("New page", "web");
      return title(browserPageLabel({ url, title: facts.page?.title }, "Browser"), "web", {
        tooltip: url,
      });
    }
  }
}
