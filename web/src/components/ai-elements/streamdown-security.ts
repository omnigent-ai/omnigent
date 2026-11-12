import { cjk } from "@streamdown/cjk";
import { createMathPlugin } from "@streamdown/math";
import { mermaid } from "./mermaidPlugin";
import { defaultRehypePlugins, type LinkSafetyConfig, type StreamdownProps } from "streamdown";
import { lazyCodePlugin } from "./lazyCodePlugin";

type StreamdownRehypePlugins = NonNullable<StreamdownProps["rehypePlugins"]>;
type StreamdownRehypePlugin = StreamdownRehypePlugins[number];
type StreamdownPluginTuple = Extract<StreamdownRehypePlugin, readonly unknown[]>;
type StreamdownHardenOptions = {
  allowedImagePrefixes: string[];
  allowedLinkPrefixes?: string[];
  allowedProtocols?: string[];
  allowDataImages?: boolean;
  defaultOrigin?: string;
};
type StreamdownHardenPlugin = StreamdownPluginTuple & {
  1: StreamdownHardenOptions;
};
type StreamdownSanitizeSchema = { protocols?: Record<string, string[] | undefined> };
type StreamdownSanitizePlugin = StreamdownPluginTuple & {
  1: StreamdownSanitizeSchema;
};

export const STREAMDOWN_PLUGINS = {
  cjk,
  code: lazyCodePlugin,
  // Only `$$…$$` opens math. A single `$` is prose far more often than it is a
  // math delimiter — currency ($5), rates ($/PR, $/session), shell variables
  // ($LLM_API_KEY) — and single-dollar math pairs any two of them up, rendering
  // the whole span between them as letter-by-letter math soup. Explicit TeX
  // delimiters (`\(…\)`, `\[…\]`), which is what LLMs emit for real math, are
  // rewritten to `$$…$$` by `normalizeExplicitMathDelimiters`.
  math: createMathPlugin({ singleDollarTextMath: false }),
  mermaid,
};
export const SECURE_STREAMDOWN_REHYPE_PLUGINS = createStreamdownRehypePlugins(false);
export const FILE_LINK_STREAMDOWN_REHYPE_PLUGINS = createStreamdownRehypePlugins(true);
export const WORKSPACE_IMAGE_STREAMDOWN_REHYPE_PLUGINS = createStreamdownRehypePlugins(
  true,
  false,
  true,
);
export const GITHUB_ATTACHMENT_STREAMDOWN_REHYPE_PLUGINS = createStreamdownRehypePlugins(
  false,
  true,
);

// An image or video GitHub hosts for a PR or issue, e.g. a pasted screenshot.
// Exact shape only, so loading it can reach nothing but GitHub's asset store.
const GITHUB_ATTACHMENT_ASSET =
  /^https:\/\/github\.com\/user-attachments\/assets\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// The only attributes a restored image keeps, so nothing like `srcset` can load elsewhere.
const GITHUB_IMAGE_ATTRIBUTES = ["src", "alt", "title", "width", "height"];
// Harden checks only `img` and `a` tags, so a parked attachment passes through it untouched.
const PARKED_GITHUB_IMAGE_TAG = "omnigent-github-image";
const PARKED_GITHUB_VIDEO_TAG = "omnigent-github-video";

// Attribute carrying the workspace path a file link names, percent-decoded
// from its (unhardened) href so it matches the real filename on disk.
export const WORKSPACE_FILE_LINK_ATTR = "data-omnigent-file";
export const WORKSPACE_IMAGE_ATTR = "data-omnigent-image";
const PARKED_WORKSPACE_IMAGE_TAG = "omnigent-workspace-image";

// A protocol, protocol-relative or in-page href, never a workspace file. A `:12`
// position is a file citation; a `:12-18` range only after a file extension
// (`notes.md:12-18`), so `skype:555-1234` (and `Makefile:12-18`) stays a link.
const NON_FILE_HREF =
  /^(?:[a-zA-Z][a-zA-Z0-9+-]*:(?!\d+(?::\d+)?$)|[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d+(?::\d+|-\d+)?$)|\/\/|#)/;
// Column and range are mutually exclusive in the colon form.
const COLON_POSITION_SUFFIX = /:(\d+)(?::(\d+)|-(\d+))?$/;
// A range's end column is matched but discarded.
const HASH_POSITION_SUFFIX = /#L(\d+)(?:C(\d+))?(?:-L?(\d+)(?:C\d+)?)?$/i;

export interface WorkspaceFileCitation {
  path: string;
  line: number | null;
  column?: number;
  /** Last line of a cited range; only set when it follows `line`. */
  endLine?: number;
  hasPosition: boolean;
}

/** Splits the line target from a local-file citation, preserving its path. */
export function splitWorkspaceFileCitation(text: string): WorkspaceFileCitation {
  const match = text.match(COLON_POSITION_SUFFIX) ?? text.match(HASH_POSITION_SUFFIX);
  if (!match || match.index === undefined) return { path: text, line: null, hasPosition: false };
  const rawLine = Number(match[1]);
  const line = Number.isSafeInteger(rawLine) && rawLine > 0 ? rawLine : null;
  const column = Number(match[2]);
  const endLine = Number(match[3]);
  return {
    path: text.slice(0, match.index),
    line,
    ...(Number.isSafeInteger(column) && column > 0 ? { column } : {}),
    ...(line !== null && Number.isSafeInteger(endLine) && endLine > line ? { endLine } : {}),
    hasPosition: true,
  };
}

/** File name plus cited position, or null when the text has no directory. */
export function shortFileCitationLabel(text: string): string | null {
  const { path } = splitWorkspaceFileCitation(text);
  const slash = path.lastIndexOf("/");
  if (slash < 0 || slash === path.length - 1) return null;
  return text.slice(slash + 1);
}

// An Omnigent session deep link, `omnigent://<host[:port]>/c/<session_id>`, which
// the desktop app registers. Matched on the raw href, so there are no
// credentials, query, fragment, or dot segments.
const OMNIGENT_SESSION_LINK =
  /^omnigent:\/\/((?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?)\/c\/([A-Za-z0-9_-]+)\/?$/;

/** The server host (`host[:port]`) and session an Omnigent session link names, or null. */
export function parseOmnigentSessionLink(href: string): { host: string; sessionId: string } | null {
  const match = OMNIGENT_SESSION_LINK.exec(href);
  if (!match) return null;
  // The pattern admits some authorities no URL can name, such as `[.]` or port 99999.
  try {
    new URL(`http://${match[1]}`);
  } catch {
    return null;
  }
  return { host: match[1].toLowerCase(), sessionId: match[2] };
}

/**
 * The in-app chat path for a session link to the server `page` is served from, or
 * null. The authority is canonicalized with the page's scheme, so a default port
 * (`example.test:443` on an HTTPS page) still matches.
 */
export function sameServerSessionPath(
  href: string,
  page: { protocol: string; host: string },
): string | null {
  const link = parseOmnigentSessionLink(href);
  if (!link || (page.protocol !== "http:" && page.protocol !== "https:")) return null;
  const linkHost = new URL(`${page.protocol}//${link.host}`).host;
  return linkHost === page.host.toLowerCase() ? `/c/${link.sessionId}` : null;
}

/** Whether `href` is exactly an Omnigent session deep link. */
export function isOmnigentSessionLink(href: string): boolean {
  return parseOmnigentSessionLink(href) !== null;
}

/**
 * Drops the href of every `omnigent:` link that is not a session deep link.
 * Sanitize admits the whole scheme; harden then marks the rest " [blocked]".
 */
export function restrictOmnigentLinks() {
  return (tree: HastElement) => {
    visitElements(tree, (node) => {
      const href = node.properties?.href;
      if (node.tagName !== "a" || typeof href !== "string") return;
      if (!/^\s*omnigent:/i.test(href) || isOmnigentSessionLink(href)) return;
      const { href: _dropped, ...rest } = node.properties ?? {};
      node.properties = rest;
    });
  };
}

// Where a file link's href is parked once the path moves to the data
// attribute. Must be a *named* fragment: harden passes a fragment-only href
// through only when `new URL(href, base).hash` round-trips, and a bare "#"
// parses to an empty hash, so it would be blocked like any unresolvable URL.
const PARKED_FILE_HREF = "#omnigent-file";

interface HastElement {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastElement[];
  value?: string;
}

// Streamdown enables a link-safety confirmation modal by default: clicking any
// markdown link pops an "Open external link?" dialog instead of following the
// link. Disable it so chat links behave like ordinary links — a plain click
// opens them and cmd/ctrl-click opens a new tab. With safety off Streamdown
// still renders the anchor as `<a target="_blank" rel="noreferrer">`, so we
// keep new-tab opening plus referrer/reverse-tabnabbing protection without the
// extra confirmation click.
export const CHAT_LINK_SAFETY: LinkSafetyConfig = { enabled: false };

/** Preserves local file URIs and basename citations through sanitization. */
export function rewriteFileUriLinks() {
  return (tree: HastElement) => {
    visitElements(tree, (node) => {
      if (node.tagName !== "a") return;
      const href = node.properties?.href;
      if (typeof href !== "string") return;
      // A basename citation looks like a URL scheme to sanitize (README.md:12).
      const citation = splitWorkspaceFileCitation(href);
      if (citation.line && /^[^:?#]+\.[\w-]+$/.test(citation.path)) {
        node.properties = {
          ...node.properties,
          href: `${citation.path}#L${citation.line}${citation.column ? `C${citation.column}` : ""}${
            citation.endLine ? `-L${citation.endLine}` : ""
          }`,
        };
        return;
      }
      if (!/^file:/i.test(href)) return;
      const path = fileUriToLocalPath(href);
      if (!path) return;
      node.properties = { ...node.properties, href: path };
    });
  };
}

/**
 * Returns a local absolute path only when rewriting preserves href meaning.
 *
 * The path is returned still percent-encoded, exactly as any other markdown
 * destination arrives, so that {@link markWorkspaceFileLinks} decodes every
 * file link once. Decoding here as well would turn a file literally named
 * `report%20final.md` (linked as `report%2520final.md`) into `report final.md`.
 */
function fileUriToLocalPath(href: string): string | null {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  if (url.protocol !== "file:" || url.hostname || url.search) return null;
  // Validate the decoded form: that is the path the FileViewer will open.
  let path: string;
  try {
    path = decodeURIComponent(url.pathname);
  } catch {
    return null;
  }
  // Keep the rewritten href absolute without introducing URL delimiters.
  if (!path.startsWith("/") || path.startsWith("//") || path === "/" || /[?#]/.test(path)) {
    return null;
  }
  if (!url.hash) return url.pathname;
  const cited = splitWorkspaceFileCitation(`${path}${url.hash}`);
  return cited.hasPosition ? `${url.pathname}${url.hash}` : null;
}

/**
 * Moves the href of every file-path link onto {@link WORKSPACE_FILE_LINK_ATTR}
 * and parks the href on an inert fragment.
 *
 * Agents routinely link a file they just wrote (`[foo.md](/abs/ws/foo.md)`).
 * Left alone, Streamdown's harden pass either keeps such a link as a real
 * anchor (so clicking it navigates the app origin and 404s), or, for a path
 * with no leading slash, strips the href and appends " [blocked]", which
 * reads as though the app censored the link. Both are wrong: the link
 * names a workspace file and should open the FileViewer.
 *
 * Running before harden hands the path to a renderer that owns the click
 * instead. A fragment href is chosen because harden passes those through
 * untouched, while an anchor with *no* href is blocked like any other
 * unresolvable URL. Only hrefs that could name a real file are moved: a URL,
 * a `mailto:`/`javascript:` scheme, or anything carrying a query or fragment
 * is left for harden to judge exactly as before.
 *
 * This pass owns percent-decoding: the stored path is decoded exactly once,
 * here, whether the href came straight from markdown (`My%20Notes.md`) or was
 * handed over still encoded by {@link rewriteFileUriLinks}. The citation
 * suffix is split off first and only the filename is decoded, so an encoded
 * delimiter in a filename is never read back as a line citation.
 */
export function markWorkspaceFileLinks() {
  return (tree: HastElement) => {
    visitElements(tree, (node) => {
      if (node.tagName !== "a") return;
      const href = node.properties?.href;
      if (typeof href !== "string" || !href) return;
      if (NON_FILE_HREF.test(href) || href.includes("?")) return;
      const cited = splitWorkspaceFileCitation(href);
      if (href.includes("#") && !cited.hasPosition) return;
      node.properties = {
        ...node.properties,
        href: PARKED_FILE_HREF,
        [WORKSPACE_FILE_LINK_ATTR]: decodeFilePath(cited.path) + href.slice(cited.path.length),
      };
    });
  };
}

/**
 * Decodes the filename unless decoding fails or creates citation syntax. A
 * lone `%` is a legal filename character, so a malformed sequence keeps the raw
 * text rather than dropping the link. The opener re-parses citations, so an
 * ambiguous name (`report.md%23L12`) must stay encoded to avoid opening a
 * sibling file.
 */
function decodeFilePath(path: string): string {
  let decoded: string;
  try {
    decoded = decodeURIComponent(path);
  } catch {
    return path;
  }
  return splitWorkspaceFileCitation(decoded).hasPosition ? path : decoded;
}

/**
 * Hides GitHub attachments from harden, which blocks every remote image. A bare
 * attachment URL on a line of its own is the one GitHub plays as a video.
 */
function parkGitHubAttachments() {
  return (tree: HastElement) => {
    visitElements(tree, (node) => {
      const src = node.properties?.src;
      if (node.tagName === "img" && typeof src === "string" && GITHUB_ATTACHMENT_ASSET.test(src)) {
        node.tagName = PARKED_GITHUB_IMAGE_TAG;
      } else if (node.tagName === "p" && node.children) {
        node.children = node.children.map((child, index, siblings) =>
          isBareGitHubAttachmentLink(child) &&
          startsLine(siblings[index - 1]) &&
          endsLine(siblings[index + 1])
            ? {
                type: "element",
                tagName: PARKED_GITHUB_VIDEO_TAG,
                properties: { src: child.properties?.href },
                children: [],
              }
            : child,
        );
      }
    });
  };
}

/** An attachment URL autolinked as itself; a titled link stays a link, as on GitHub. */
function isBareGitHubAttachmentLink(node: HastElement): boolean {
  const href = node.properties?.href;
  const [text, ...rest] = node.children ?? [];
  return (
    node.tagName === "a" &&
    typeof href === "string" &&
    GITHUB_ATTACHMENT_ASSET.test(href) &&
    rest.length === 0 &&
    text?.type === "text" &&
    text.value === href
  );
}

function startsLine(previous: HastElement | undefined): boolean {
  return (
    !previous ||
    previous.tagName === "br" ||
    (previous.type === "text" && /\n[ \t]*$/.test(previous.value ?? ""))
  );
}

function endsLine(next: HastElement | undefined): boolean {
  return (
    !next || next.tagName === "br" || (next.type === "text" && /^[ \t]*\n/.test(next.value ?? ""))
  );
}

/**
 * Restores the attachments {@link parkGitHubAttachments} parked: images sent
 * without a referrer, and videos as players that load only their metadata.
 */
function restoreGitHubAttachments() {
  return (tree: HastElement) => {
    visitElements(tree, (node) => {
      if (node.tagName === PARKED_GITHUB_IMAGE_TAG) {
        node.tagName = "img";
        const kept = GITHUB_IMAGE_ATTRIBUTES.filter(
          (name) => node.properties?.[name] !== undefined,
        );
        node.properties = {
          ...Object.fromEntries(kept.map((name) => [name, node.properties?.[name]])),
          referrerPolicy: "no-referrer",
        };
      } else if (node.tagName === PARKED_GITHUB_VIDEO_TAG) {
        // Media elements take no referrer policy, so the page's default applies.
        node.tagName = "video";
        node.properties = { src: node.properties?.src, controls: true, preload: "metadata" };
      }
    });
  };
}

/** Preserve local image paths for authenticated rendering, never as browser URLs. */
function parkWorkspaceImages() {
  return (tree: HastElement) => {
    visitElements(tree, (node) => {
      const src = node.properties?.src;
      if (node.tagName !== "img" || typeof src !== "string" || !src) return;
      let path: string;
      try {
        path = decodeURIComponent(src);
      } catch {
        return;
      }
      if (/^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(path.trim()) || /[?#\\]/.test(path)) return;
      const { alt, title } = node.properties ?? {};
      node.tagName = PARKED_WORKSPACE_IMAGE_TAG;
      node.properties = {
        [WORKSPACE_IMAGE_ATTR]: path,
        alt: typeof alt === "string" ? alt : undefined,
        title: typeof title === "string" ? title : undefined,
      };
    });
  };
}

function restoreWorkspaceImages() {
  return (tree: HastElement) => {
    visitElements(tree, (node) => {
      if (node.tagName === PARKED_WORKSPACE_IMAGE_TAG) node.tagName = "img";
    });
  };
}

function visitElements(node: HastElement, visitor: (node: HastElement) => void): void {
  if (node.type === "element") visitor(node);
  for (const child of node.children ?? []) {
    visitElements(child, visitor);
  }
}

function isStreamdownHardenPlugin(
  plugin: StreamdownRehypePlugin,
): plugin is StreamdownHardenPlugin {
  return Array.isArray(plugin) && plugin.length >= 2 && isHardenOptions(plugin[1]);
}

function isStreamdownSanitizePlugin(
  plugin: StreamdownRehypePlugin,
): plugin is StreamdownSanitizePlugin {
  return (
    Array.isArray(plugin) &&
    plugin.length >= 2 &&
    typeof plugin[1] === "object" &&
    plugin[1] !== null
  );
}

/** Sanitize with `omnigent:` hrefs admitted, narrowed to session links right after. */
function withOmnigentSessionLinks(plugin: StreamdownRehypePlugin): StreamdownRehypePlugins {
  if (!isStreamdownSanitizePlugin(plugin)) {
    throw new Error("Streamdown sanitize plugin must be a [plugin, schema] tuple");
  }
  const schema = plugin[1];
  // A schema without an href allowlist allows every scheme; adding one would block web links.
  const hrefProtocols = schema.protocols?.href;
  if (!hrefProtocols) {
    throw new Error("Streamdown sanitize schema must restrict href protocols");
  }
  return [
    [
      plugin[0],
      {
        ...schema,
        protocols: { ...schema.protocols, href: [...hrefProtocols, "omnigent"] },
      },
    ] satisfies StreamdownPluginTuple,
    restrictOmnigentLinks,
  ];
}

function isHardenOptions(value: unknown): value is StreamdownHardenOptions {
  return (
    typeof value === "object" &&
    value !== null &&
    "allowedImagePrefixes" in value &&
    Array.isArray(value.allowedImagePrefixes)
  );
}

function createStreamdownRehypePlugins(
  markFileLinks: boolean,
  allowGitHubAttachmentImages = false,
  allowWorkspaceImages = false,
): StreamdownRehypePlugins {
  const plugins: StreamdownRehypePlugins = [];
  let sawSanitize = false;

  for (const [key, plugin] of Object.entries(defaultRehypePlugins)) {
    // Preserve local file URIs and line citations before sanitization.
    if (key === "sanitize") {
      sawSanitize = true;
      if (markFileLinks) plugins.push(rewriteFileUriLinks);
      plugins.push(...withOmnigentSessionLinks(plugin));
      continue;
    }
    if (key !== "harden") {
      plugins.push(plugin);
      continue;
    }

    // Immediately before harden, and so after Streamdown's `sanitize` step,
    // which drops the marker attribute as unknown. Sanitize has also already
    // stripped any dangerous href, leaving nothing to hand over for such a link.
    if (markFileLinks) {
      plugins.push(markWorkspaceFileLinks);
    }
    if (allowGitHubAttachmentImages) plugins.push(parkGitHubAttachments);
    if (allowWorkspaceImages) plugins.push(parkWorkspaceImages);

    if (!isStreamdownHardenPlugin(plugin)) {
      throw new Error("Streamdown harden plugin must be a [plugin, options] tuple");
    }

    plugins.push([
      plugin[0],
      { ...plugin[1], allowedImagePrefixes: [] },
    ] satisfies StreamdownPluginTuple);
    if (allowGitHubAttachmentImages) plugins.push(restoreGitHubAttachments);
    if (allowWorkspaceImages) plugins.push(restoreWorkspaceImages);
  }

  // Sanitize must run first: it drops raw HTML that could forge a parked attachment.
  if ((markFileLinks || allowGitHubAttachmentImages || allowWorkspaceImages) && !sawSanitize) {
    throw new Error("Streamdown default rehype plugins carry no sanitize step");
  }

  return plugins;
}
