// Parks PR references (`#1234`, `owner/repo#12`) on fragment links that the anchor
// override resolves once session info loads; Streamdown won't re-parse a settled message.

import type { PullRequestInfo } from "@/hooks/usePullRequests";

/**
 * Href prefix of a parked reference, then `1234` or `owner/repo/12`. The per-load
 * nonce keeps a hand-written `#omnigent-pr-…` link, on any surface, from resolving.
 */
export const PULL_REQUEST_REF_HREF_PREFIX = `#omnigent-pr-${Math.random().toString(36).slice(2, 10)}-`;

export interface PullRequestRef {
  /** `owner/repo` written with the number, or null for a bare `#1234`. */
  repo: string | null;
  number: string;
}

// Unicode letters, digits and marks on either side block a reference, as does an
// extension (`#4242.md`); Han, kana and Hangul may border one outside a path segment.
// Bare numbers need 4+ digits. A captured prefix replaces lookbehind for Safari < 16.4.
const UNSPACED_SCRIPT = "\\p{scx=Han}\\p{scx=Hiragana}\\p{scx=Katakana}\\p{scx=Hangul}";
export const PULL_REQUEST_REF = new RegExp(
  `(^|[^\\p{L}\\p{N}\\p{M}_&/#.-]|[${UNSPACED_SCRIPT}])` +
    "(?:([A-Za-z0-9][A-Za-z0-9-]{0,38})\\/([A-Za-z0-9._-]{1,100}))?#(\\d+)" +
    `(?!(?![${UNSPACED_SCRIPT}])[\\p{L}\\p{N}\\p{M}_]|\\.[\\p{L}\\p{N}])`,
  "gu",
);
const UNSPACED_CHAR = new RegExp(`^[${UNSPACED_SCRIPT}]$`, "u");
const PATH_SEPARATOR = /[/\\]/;
const BARE_NUMBER = /^[1-9]\d{3,9}$/;
const EXPLICIT_NUMBER = /^[1-9]\d{0,9}$/;
// GitHub owners can't end in `-`.
const GITHUB_OWNER = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$/;
const GITHUB_REPO = /^[A-Za-z0-9._-]{1,100}$/;
// `docs/setup.md#3` and `assets/logo.png#3` are file paths; an extensionless `src/foo#12`
// still links. This also leaves dotted repos like `vercel/next.js#1` as text; a wrong link
// is worse than none.
const FILE_EXTENSION = /[^.]\.[A-Za-z0-9]+$/;
// Prose (`and/or#1`, `24/7#1`), git refs (`origin/main#1`, `feature/login#1`) and
// paths (`docs/README#1`) that read as `owner/repo`.
const NON_OWNER =
  /^(\d+|origin|upstream|and|or|src|lib|docs?|tests?|feature|fix|bugfix|hotfix|release|chore)$/i;

/** Repo for bare references when the session's repo is unknown; null leaves them as text. */
const FALLBACK_PULL_REQUEST_REPO: string | null = null;

interface MarkdownNode {
  type: string;
  value?: string;
  url?: string;
  title?: string | null;
  children?: MarkdownNode[];
  position?: { start: { offset?: number }; end: { offset?: number } };
}

// Their text is already a link. Code and raw HTML carry a `value`, not text children.
const LINK_NODE_TYPES = new Set(["link", "linkReference"]);
// Track raw anchor and code-like tags; their text parses as sibling nodes, while an
// HTML block holds its own closers. As in a browser, `<a/>` still opens its element.
const HTML_OPAQUE_TAG = /<(\/?)(a|code|pre|kbd|samp)(?=[\s/>])/gi;

// Tokens of a text node's source, in order. Each `#` of the value is a literal `#`
// or an escaped one; other escapes and references (`&#1234;`) hold no `#`.
const SOURCE_TOKEN =
  /\\#|&#0{0,5}35;|&#[xX]0{0,4}23;|&num;|\\[!-/:-@[-`{-~]|&#\d{1,7};|&#[xX][\dA-Fa-f]{1,6};|#/g;
const ESCAPED_HASH = /^(?:\\#|&#0{0,5}35;|&#[xX]0{0,4}23;|&num;)$/;

/** Remark plugin parking every pull request reference in a text node on a fragment link. */
export function remarkPullRequestRefs() {
  return (tree: MarkdownNode, file?: { value?: unknown }) =>
    linkifyChildren(tree, typeof file?.value === "string" ? file.value : undefined);
}

/**
 * Per `#` in a text node's value, whether its author escaped it. Null without a
 * source file, as when the plugin runs on a tree it didn't parse.
 */
function escapedHashes(node: MarkdownNode, source: string | undefined): boolean[] | null {
  if (source === undefined) return null;
  const hashes = (node.value?.split("#").length ?? 1) - 1;
  const start = node.position?.start.offset;
  const end = node.position?.end.offset;
  const raw = start === undefined || end === undefined ? null : source.slice(start, end);
  if (raw === node.value) return null;
  const escaped: boolean[] = [];
  for (const [token] of raw?.matchAll(SOURCE_TOKEN) ?? []) {
    if (token === "#") escaped.push(false);
    else if (ESCAPED_HASH.test(token)) escaped.push(true);
  }
  // A node the source can't place, or that this pass misreads, stays text.
  return raw !== null && escaped.length === hashes
    ? escaped
    : Array.from({ length: hashes }, () => true);
}

function linkifyChildren(node: MarkdownNode, source?: string): void {
  if (!node.children || LINK_NODE_TYPES.has(node.type)) return;
  const openOpaqueTags = new Map<string, number>();
  node.children = node.children.flatMap((child) => {
    if (child.type === "html" && typeof child.value === "string") {
      for (const [, closing, tagName] of child.value.matchAll(HTML_OPAQUE_TAG)) {
        const name = tagName.toLowerCase();
        const open = openOpaqueTags.get(name) ?? 0;
        openOpaqueTags.set(name, closing ? Math.max(0, open - 1) : open + 1);
      }
      return [child];
    }
    if ([...openOpaqueTags.values()].some((open) => open > 0)) return [child];
    if (child.type === "text" && typeof child.value === "string") {
      return splitPullRequestRefs(child.value, escapedHashes(child, source));
    }
    linkifyChildren(child, source);
    return [child];
  });
}

/**
 * Splits `text` into text and parked-link nodes; a single text node when nothing matches.
 * `escaped` flags each `#` of `text` the author escaped, which starts no reference.
 */
export function splitPullRequestRefs(
  text: string,
  escaped: readonly boolean[] | null = null,
): MarkdownNode[] {
  if (!text.includes("#")) return [{ type: "text", value: text }];
  const nodes: MarkdownNode[] = [];
  let last = 0;
  // `#`s before `scanned`; matches only move forward, so counting stays one pass.
  let hashesBefore = 0;
  let scanned = 0;
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
    if (!valid || followsPathSegment(text, match.index, prefix)) continue;
    if (escaped) {
      const hash = start + token.indexOf("#");
      for (
        let at = text.indexOf("#", scanned);
        at !== -1 && at < hash;
        at = text.indexOf("#", at + 1)
      ) {
        hashesBefore += 1;
      }
      scanned = hash;
      if (escaped[hashesBefore]) continue;
    }
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

/** Whether an unspaced-script `prefix` at `index` ends a run that follows a path separator. */
function followsPathSegment(text: string, index: number, prefix: string): boolean {
  if (!UNSPACED_CHAR.test(prefix)) return false;
  let start = index;
  for (;;) {
    // Step back a whole code point, so a supplementary character (`𠀀`) continues the run.
    const low = text.charCodeAt(start - 1);
    const width = low >= 0xdc00 && low <= 0xdfff && start >= 2 ? 2 : 1;
    const previous = text.slice(start - width, start);
    if (start === 0 || !UNSPACED_CHAR.test(previous)) {
      return PATH_SEPARATOR.test(previous);
    }
    start -= width;
  }
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

/** The github.com repo whose numbers `nameWithOwner`'s references use. */
function canonicalRepo(nameWithOwner: string): string | null {
  return nameWithOwner;
}

/**
 * The workspace's github.com repo for bare references; null leaves them as text
 * (Enterprise, other forges, older runners).
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
