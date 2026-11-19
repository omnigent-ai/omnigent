// Pull request references (`#1234`, `owner/repo#12`) in chat markdown.
//
// A remark pass turns each reference into a link parked on a named fragment;
// the anchor override resolves it against the session's repo at render time.
// Parsing can't resolve it: Streamdown never re-parses a settled message when
// its plugins change, and the session's repo usually loads after first paint.

import type { PullRequestInfo } from "@/hooks/usePullRequests";

/** Href prefix of a parked reference: `#omnigent-pr-1234` or `#omnigent-pr-owner/repo/12`. */
export const PULL_REQUEST_REF_HREF_PREFIX = "#omnigent-pr-";

export interface PullRequestRef {
  /** `owner/repo` written with the number, or null for a bare `#1234`. */
  repo: string | null;
  number: string;
}

// The `#` must start a token: `&#1234;`, `a#1234`, `/x#1234` and `##1234` are
// not references. Bare numbers need 4+ digits so prose like "step #3" stays
// text; a trailing word character (`#1234abc`) rules the token out. The
// boundary is a captured prefix, not a lookbehind, which Safari < 16.4 rejects.
export const PULL_REQUEST_REF =
  /(^|[^\w&/#.-])(?:([A-Za-z0-9][A-Za-z0-9-]{0,38})\/([A-Za-z0-9._-]{1,100}))?#(\d+)(?!\w)/g;
const BARE_NUMBER = /^[1-9]\d{3,9}$/;
const EXPLICIT_NUMBER = /^[1-9]\d{0,9}$/;
const GITHUB_OWNER = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/;
const GITHUB_REPO = /^[A-Za-z0-9._-]{1,100}$/;
// `docs/setup.md#3` and `assets/logo.png#3` are file paths; an extensionless `src/foo#12`
// still links. This also leaves dotted repos like `vercel/next.js#1` as text; a wrong link
// is worse than none.
const FILE_EXTENSION = /[^.]\.[A-Za-z0-9]+$/;
// Prose (`and/or#1`, `24/7#1`) and git refs (`origin/main#1`) that read as `owner/repo`.
const NON_OWNER = /^(\d+|origin|upstream|and|or)$/i;

/** Repo for bare references when the session's repo is unknown; null leaves them as text. */
const FALLBACK_PULL_REQUEST_REPO: string | null = null;

interface MarkdownNode {
  type: string;
  value?: string;
  url?: string;
  title?: string | null;
  children?: MarkdownNode[];
}

// Their text is already a link. Code and raw HTML carry a `value`, not text children.
const LINK_NODE_TYPES = new Set(["link", "linkReference"]);
// Inline raw HTML parses as separate tag and text siblings: `<a href>`, `#1234`, `</a>`.
// Text inside a raw anchor is already a link; inside raw code it is literal.
const HTML_OPAQUE_OPEN = /^<(?:a|code|pre|kbd|samp)[\s/>]/i;
const HTML_OPAQUE_CLOSE = /^<\/(?:a|code|pre|kbd|samp)\s*>/i;

/** Remark plugin parking every pull request reference in a text node on a fragment link. */
export function remarkPullRequestRefs() {
  return linkifyChildren;
}

function linkifyChildren(node: MarkdownNode): void {
  if (!node.children || LINK_NODE_TYPES.has(node.type)) return;
  let openOpaqueTags = 0;
  node.children = node.children.flatMap((child) => {
    if (child.type === "html" && typeof child.value === "string") {
      if (HTML_OPAQUE_OPEN.test(child.value)) openOpaqueTags += 1;
      else if (HTML_OPAQUE_CLOSE.test(child.value)) {
        openOpaqueTags = Math.max(0, openOpaqueTags - 1);
      }
      return [child];
    }
    if (openOpaqueTags > 0) return [child];
    if (child.type === "text" && typeof child.value === "string") {
      return splitPullRequestRefs(child.value);
    }
    linkifyChildren(child);
    return [child];
  });
}

/** Splits `text` into text and parked-link nodes; a single text node when nothing matches. */
export function splitPullRequestRefs(text: string): MarkdownNode[] {
  if (!text.includes("#")) return [{ type: "text", value: text }];
  const nodes: MarkdownNode[] = [];
  let last = 0;
  for (const match of text.matchAll(PULL_REQUEST_REF)) {
    const [whole, prefix, owner, repo, number] = match;
    const token = whole.slice(prefix.length);
    const start = match.index + prefix.length;
    const explicit = owner !== undefined;
    const valid = explicit
      ? EXPLICIT_NUMBER.test(number) &&
        isGitHubRepo(`${owner}/${repo}`) &&
        !FILE_EXTENSION.test(repo) &&
        !NON_OWNER.test(owner)
      : BARE_NUMBER.test(number);
    if (!valid) continue;
    if (start > last) nodes.push({ type: "text", value: text.slice(last, start) });
    nodes.push({
      type: "link",
      url: `${PULL_REQUEST_REF_HREF_PREFIX}${explicit ? `${owner}/${repo}/` : ""}${number}`,
      title: null,
      children: [{ type: "text", value: token }],
    });
    last = start + token.length;
  }
  if (last < text.length || nodes.length === 0) {
    nodes.push({ type: "text", value: text.slice(last) });
  }
  return nodes;
}

/** The reference a parked href names, or null for any other href. */
export function parsePullRequestRefHref(href: string | undefined): PullRequestRef | null {
  if (!href?.startsWith(PULL_REQUEST_REF_HREF_PREFIX)) return null;
  const rest = href.slice(PULL_REQUEST_REF_HREF_PREFIX.length);
  if (BARE_NUMBER.test(rest)) return { repo: null, number: rest };
  const explicit = rest.match(/^([^/]+\/[^/]+)\/(\d+)$/);
  if (!explicit || !EXPLICIT_NUMBER.test(explicit[2]) || !isGitHubRepo(explicit[1])) return null;
  return { repo: explicit[1], number: explicit[2] };
}

function isGitHubRepo(nameWithOwner: string): boolean {
  const [owner, repo, ...extra] = nameWithOwner.split("/");
  return (
    extra.length === 0 &&
    GITHUB_OWNER.test(owner) &&
    repo !== undefined &&
    GITHUB_REPO.test(repo) &&
    repo !== "." &&
    repo !== ".."
  );
}

/** The github.com repo whose numbers `nameWithOwner`'s references use; currently the repo itself. */
function canonicalRepo(nameWithOwner: string): string | null {
  return nameWithOwner;
}

/**
 * The github.com repo a bare reference in this session points at, or null to
 * leave it as text. It is the workspace's own repo: `repo` names a tracked PR's,
 * which can live in another repo or on another host. Only github.com workspaces
 * link, as an Enterprise repo's name could land on an unrelated (possibly
 * squatted) github.com repo; older servers that don't report the workspace's
 * repo leave references as text.
 */
export function sessionPullRequestRepo(
  info: PullRequestInfo | undefined,
  fallback: string | null = FALLBACK_PULL_REQUEST_REPO,
): string | null {
  if (!isGitHubWorkspace(info)) return null;
  const own = info?.workspace_repo?.name_with_owner;
  if (own) return isGitHubRepo(own) ? canonicalRepo(own) : null;
  return fallback;
}

function isGitHubWorkspace(info: PullRequestInfo | undefined): boolean {
  return info?.workspace_repo?.host === "github.com";
}

/** Where `ref` links in this session, or null when it should stay text. */
export function pullRequestRefUrl(
  ref: PullRequestRef,
  info: PullRequestInfo | undefined,
  fallback: string | null = FALLBACK_PULL_REQUEST_REPO,
): string | null {
  if (ref.repo !== null && !isGitHubWorkspace(info)) return null;
  const repo = ref.repo !== null ? canonicalRepo(ref.repo) : sessionPullRequestRepo(info, fallback);
  return repo ? gitHubPullRequestUrl(repo, ref.number) : null;
}

/** `https://github.com/<owner>/<repo>/pull/<n>`, or null unless every part is well-formed. */
export function gitHubPullRequestUrl(nameWithOwner: string, number: string): string | null {
  if (!isGitHubRepo(nameWithOwner) || !EXPLICIT_NUMBER.test(number)) return null;
  const path = `/${nameWithOwner}/pull/${number}`;
  try {
    const url = new URL(`https://github.com${path}`);
    const exact =
      url.protocol === "https:" &&
      url.host === "github.com" &&
      url.pathname === path &&
      !url.search &&
      !url.hash;
    return exact ? url.href : null;
  } catch {
    return null;
  }
}
