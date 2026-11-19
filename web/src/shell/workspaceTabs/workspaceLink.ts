// Workspace links as text: `omnigent://<kind>[/<id>]?<params>` for Omnigent
// places, and bare http(s) URLs for browser pages. Parsing is purely syntactic
// (no workspace root or code hosts; see workspaceLinkResolver.ts), and
// formatting is canonical, so parse(format(link)) gives back the same link.

import { canonicalPullRequestUrl } from "@/lib/pullRequestReference";

import { foldPullRequestSubpage } from "./pullRequestSubpage";
import type {
  ExplicitRange,
  LineRange,
  PullRequestView,
  WorkspaceLink,
  WorkspacePlace,
} from "./workspacePlace";

const SCHEME = "omnigent://";
const PULL_REQUEST_VIEWS: readonly PullRequestView[] = [
  "summary",
  "comments",
  "commits",
  "changes",
];
const COMMIT_PATTERN = /^[0-9a-f]{4,64}$/i;
const POSITIVE_INTEGER = /^[1-9][0-9]*$/;

export type ParsedWorkspaceLink =
  | {
      ok: true;
      link: WorkspaceLink;
      /** Parameters this kind doesn't take, dropped from the link. */
      ignored: string[];
    }
  | { ok: false; reason: string };

class LinkError extends Error {}

function refuse(reason: string): never {
  throw new LinkError(reason);
}

/** Collapse `.` and `..` segments; refuse paths that climb above their root. */
export function normalizeLinkPath(raw: string): string {
  if (raw.includes("\0")) refuse("A path can't contain a NUL character.");
  const absolute = raw.startsWith("/");
  const segments: string[] = [];
  for (const segment of raw.split("/")) {
    if (segment === "" || segment === ".") continue;
    if (segment === "..") {
      if (segments.length === 0) refuse(`The path “${raw}” goes above its folder.`);
      segments.pop();
      continue;
    }
    segments.push(segment);
  }
  if (!absolute && segments.length === 0) refuse("Name a file or folder in the path.");
  return (absolute ? "/" : "") + segments.join("/");
}

function parsePositive(value: string, name: string): number {
  const number = Number(value);
  if (!POSITIVE_INTEGER.test(value) || !Number.isSafeInteger(number)) {
    refuse(`“${value}” isn't a valid ${name}.`);
  }
  return number;
}

/** `12` or `12-20`, plus an optional column of the first line. */
function parseLineRange(line: string | null, column: string | null): LineRange | undefined {
  if (line === null) {
    if (column !== null) refuse("A column needs a line.");
    return undefined;
  }
  const match = /^([^-]+)(?:-(.+))?$/.exec(line);
  if (!match) refuse(`“${line}” isn't a valid line range.`);
  const start = parsePositive(match[1], "line");
  const range: LineRange = { start };
  if (match[2] !== undefined) {
    const end = parsePositive(match[2], "line");
    if (end < start) refuse(`The line range “${line}” ends before it starts.`);
    if (end !== start) range.end = end;
  }
  if (column !== null) range.column = parsePositive(column, "column");
  return range;
}

function parseWebUrl(raw: string): string {
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    refuse("That web address isn't valid.");
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    refuse("Only http and https pages open in the browser.");
  }
  return parsed.href;
}

function parsePullRequestUrl(raw: string | null): string {
  if (raw === null || raw === "") refuse("A pull request link needs its url.");
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    refuse("That pull request link isn't valid.");
  }
  if (parsed.protocol !== "https:") refuse("A pull request link must use https.");
  return parsed.href;
}

/** Reads parameters once each, so whatever is left over can be reported. */
class Params {
  private readonly params: URLSearchParams;
  private readonly used = new Set<string>();

  constructor(query: string) {
    this.params = new URLSearchParams(query);
  }

  get(name: string): string | null {
    this.used.add(name);
    const value = this.params.get(name);
    return value === null || value === "" ? null : value;
  }

  path(name: string): string | undefined {
    const value = this.get(name);
    return value === null ? undefined : normalizeLinkPath(value);
  }

  ignored(): string[] {
    return [...new Set(this.params.keys())].filter((name) => !this.used.has(name));
  }
}

function needsId(kind: string, id: string | undefined): string {
  if (id === undefined || id === "")
    refuse(`A ${kind} link needs an ID, as in omnigent://${kind}/<id>.`);
  return id;
}

function noId(kind: string, id: string | undefined): void {
  if (id !== undefined) refuse(`A ${kind} link doesn't take an ID.`);
}

function parsePlace(kind: string, id: string | undefined, params: Params): WorkspacePlace {
  switch (kind) {
    case "new": {
      noId(kind, id);
      const app = params.get("app");
      if (app !== null && app !== "terminal") refuse(`The new-tab page has no app “${app}”.`);
      return app === "terminal" ? { kind, app } : { kind };
    }
    case "files": {
      noId(kind, id);
      const place: WorkspacePlace = { kind };
      const path = params.path("path");
      const line = parseLineRange(params.get("line"), params.get("column"));
      const comment = params.get("comment");
      if (path !== undefined) place.path = path;
      if (line !== undefined) place.line = line;
      if (comment !== null) place.comment = comment;
      return place;
    }
    case "changes": {
      noId(kind, id);
      const place: WorkspacePlace = { kind };
      const path = params.path("path");
      const line = parseLineRange(params.get("line"), params.get("column"));
      const comment = params.get("comment");
      const base = params.get("base");
      const head = params.get("head");
      if (head !== null && base === null) refuse("A comparison with a head needs a base too.");
      if (path !== undefined) place.path = path;
      if (path !== undefined && params.get("file-tab") === "1") place.fileTab = true;
      if (line !== undefined) place.line = line;
      if (comment !== null) place.comment = comment;
      if (base !== null) {
        const range: ExplicitRange = { base };
        if (head !== null) range.head = head;
        place.range = range;
      }
      return place;
    }
    case "pulls":
    case "agents":
    case "tasks":
      noId(kind, id);
      return { kind };
    case "pull": {
      noId(kind, id);
      // A sub-page URL (…/files, …/checks) names a view; an explicit `view` wins over it.
      const folded = foldPullRequestSubpage(parsePullRequestUrl(params.get("url")));
      const explicitView = params.get("view");
      const view = explicitView ?? folded.view ?? "summary";
      if (!PULL_REQUEST_VIEWS.some((candidate) => candidate === view)) {
        refuse(`A pull request has no view “${view}”.`);
      }
      const place: WorkspacePlace = {
        kind,
        url: canonicalPullRequestUrl(folded.url),
        view: view as PullRequestView,
      };
      const focus =
        params.get("focus") ?? (explicitView === null ? folded.focus : undefined) ?? null;
      if (focus !== null) {
        if (focus !== "checks") refuse(`A pull request has no section “${focus}”.`);
        place.focus = focus;
      }
      const commit = params.get("commit") ?? folded.commit ?? null;
      if (commit !== null) {
        if (!COMMIT_PATTERN.test(commit)) refuse(`“${commit}” isn't a commit SHA.`);
        place.commit = commit.toLowerCase();
      }
      const path = params.path("path");
      const line = parseLineRange(params.get("line"), params.get("column"));
      if (path !== undefined) place.path = path;
      if (line !== undefined) place.line = line;
      return place;
    }
    case "terminal":
      return { kind, key: needsId(kind, id) };
    case "agent":
      return { kind, id: needsId(kind, id) };
    case "chat":
      return id === undefined || id === "" ? { kind } : { kind, id };
    case "search": {
      noId(kind, id);
      const query = params.get("q");
      return query === null ? { kind } : { kind, query };
    }
    case "web": {
      noId(kind, id);
      const url = params.get("url");
      return url === null ? { kind } : { kind, url: parseWebUrl(url) };
    }
    default:
      refuse(`Omnigent has no workspace place “${kind}”.`);
  }
}

function decodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    refuse("That link has broken percent-encoding.");
  }
}

function parseOmnigentLink(text: string): { link: WorkspaceLink; ignored: string[] } {
  const rest = text.slice(SCHEME.length);
  const hashAt = rest.indexOf("#");
  const withoutHash = hashAt === -1 ? rest : rest.slice(0, hashAt);
  const queryAt = withoutHash.indexOf("?");
  const pathPart = queryAt === -1 ? withoutHash : withoutHash.slice(0, queryAt);
  const query = queryAt === -1 ? "" : withoutHash.slice(queryAt + 1);
  const segments = pathPart.split("/").filter((segment, index) => index === 0 || segment !== "");
  if (segments.length > 2) refuse("A workspace link names a place and at most one ID.");
  const kind = segments[0].toLowerCase();
  const id = segments[1] === undefined ? undefined : decodeSegment(segments[1]);
  const params = new Params(query);
  const session = params.get("session");
  const tab = params.get("tab");
  const link: WorkspaceLink = { place: parsePlace(kind, id, params) };
  if (session !== null) link.session = session;
  if (tab !== null) link.tab = tab;
  return { link, ignored: params.ignored() };
}

/** Parse a workspace link or a bare http(s) page address. */
export function parseWorkspaceLink(text: string): ParsedWorkspaceLink {
  const trimmed = text.trim();
  try {
    if (trimmed.slice(0, SCHEME.length).toLowerCase() === SCHEME) {
      return { ok: true, ...parseOmnigentLink(trimmed) };
    }
    if (/^https?:\/\//i.test(trimmed)) {
      return { ok: true, link: { place: { kind: "web", url: parseWebUrl(trimmed) } }, ignored: [] };
    }
    return {
      ok: false,
      reason: "That isn't a workspace link. It should start with omnigent:// or https://.",
    };
  } catch (error) {
    if (error instanceof LinkError) return { ok: false, reason: error.message };
    throw error;
  }
}

/** Percent-encode a value, keeping `/` readable in paths. */
function encodeValue(value: string, keepSlashes: boolean): string {
  const encoded = encodeURIComponent(value);
  return keepSlashes ? encoded.replace(/%2F/g, "/") : encoded;
}

function formatLine(line: LineRange): [string, string][] {
  const range =
    line.end !== undefined && line.end !== line.start
      ? `${line.start}-${line.end}`
      : `${line.start}`;
  const entries: [string, string][] = [["line", range]];
  if (line.column !== undefined) entries.push(["column", `${line.column}`]);
  return entries;
}

/** Each place's parameters, in canonical order, with defaults left out. */
function placeParams(place: WorkspacePlace): [string, string][] {
  switch (place.kind) {
    case "new":
      return place.app ? [["app", place.app]] : [];
    case "files":
    case "changes": {
      const entries: [string, string][] = [];
      if (place.path !== undefined) entries.push(["path", place.path]);
      if (place.line) entries.push(...formatLine(place.line));
      if (place.comment !== undefined) entries.push(["comment", place.comment]);
      if (place.kind === "changes" && place.range) {
        entries.push(["base", place.range.base]);
        if (place.range.head !== undefined) entries.push(["head", place.range.head]);
      }
      if (place.kind === "changes" && place.fileTab && place.path !== undefined) {
        entries.push(["file-tab", "1"]);
      }
      return entries;
    }
    case "pull": {
      const entries: [string, string][] = [["url", place.url]];
      if (place.view !== "summary") entries.push(["view", place.view]);
      if (place.focus) entries.push(["focus", place.focus]);
      if (place.commit !== undefined) entries.push(["commit", place.commit]);
      if (place.path !== undefined) entries.push(["path", place.path]);
      if (place.line) entries.push(...formatLine(place.line));
      return entries;
    }
    case "search":
      return place.query !== undefined ? [["q", place.query]] : [];
    case "web":
      return place.url !== undefined ? [["url", place.url]] : [];
    default:
      return [];
  }
}

function placeId(place: WorkspacePlace): string | undefined {
  switch (place.kind) {
    case "terminal":
      return place.key;
    case "agent":
      return place.id;
    case "chat":
      return place.id;
    default:
      return undefined;
  }
}

/** The canonical text of a link. A web page without addressing is its own URL. */
export function formatWorkspaceLink(link: WorkspaceLink): string {
  const { place } = link;
  if (
    place.kind === "web" &&
    place.url !== undefined &&
    link.session === undefined &&
    link.tab === undefined
  ) {
    return place.url;
  }
  const entries = placeParams(place);
  if (link.session !== undefined) entries.push(["session", link.session]);
  if (link.tab !== undefined) entries.push(["tab", link.tab]);
  const id = placeId(place);
  const query = entries
    .map(([name, value]) => `${name}=${encodeValue(value, name === "path")}`)
    .join("&");
  return `${SCHEME}${place.kind}${id === undefined ? "" : `/${encodeURIComponent(id)}`}${query ? `?${query}` : ""}`;
}
