// GitHub's `:shortcode:` emoji, as its comments, reviews and descriptions render them: every name
// and alias in GitHub's emoji list becomes its glyph, in Markdown and in raw HTML alike, and
// GitHub's own picture emoji such as :shipit: become their images. Code never changes, and
// neither does a bare URL GitHub linked by itself. Unknown names stay as typed.

import { gemoji } from "gemoji";
import type { Element, ElementContent, Root as HastRoot, RootContent as HastContent } from "hast";

/** Each GitHub emoji name and alias, with its glyph. */
const GLYPHS: ReadonlyMap<string, string> = new Map(
  gemoji.flatMap(({ emoji, names }) => names.map((name) => [name, emoji] as const)),
);

/** GitHub's emoji that are pictures rather than Unicode characters. */
export const GITHUB_IMAGE_EMOJI: ReadonlySet<string> = new Set([
  "atom",
  "basecamp",
  "basecampy",
  "bowtie",
  "dependabot",
  "electron",
  "feelsgood",
  "finnadie",
  "fishsticks",
  "goberserk",
  "godmode",
  "hurtrealbad",
  "neckbeard",
  "octocat",
  "rage1",
  "rage2",
  "rage3",
  "rage4",
  "shipit",
  "suspect",
  "trollface",
]);

/** Where GitHub serves picture emoji `name`. */
export function githubImageEmojiUrl(name: string): string {
  return `https://github.githubassets.com/images/icons/emoji/${name}.png`;
}

/** A run of text, glyphs already in place, or one of GitHub's picture emoji. */
export type GitHubEmojiPart = { text: string } | { image: string };

/** Split `text` around its shortcodes: known glyphs replace theirs, picture emoji stand alone. */
export function splitGitHubEmoji(text: string): GitHubEmojiPart[] {
  const parts: GitHubEmojiPart[] = [];
  const shortcode = /:([a-z0-9_+-]+):/g;
  let pending = "";
  let copiedTo = 0;
  for (let match = shortcode.exec(text); match !== null; match = shortcode.exec(text)) {
    const name = match[1]!;
    const glyph = GLYPHS.get(name);
    if (glyph === undefined && !GITHUB_IMAGE_EMOJI.has(name)) {
      // Its closing colon may open the next shortcode, as in `10:30:+1:`.
      shortcode.lastIndex = match.index + match[0].length - 1;
      continue;
    }
    pending += text.slice(copiedTo, match.index);
    copiedTo = match.index + match[0].length;
    if (glyph !== undefined) {
      pending += glyph;
      continue;
    }
    if (pending) parts.push({ text: pending });
    pending = "";
    parts.push({ image: name });
  }
  pending += text.slice(copiedTo);
  if (pending || parts.length === 0) parts.push({ text: pending });
  return parts;
}

/** A tree node, Markdown or HTML; only the fields this reads. */
interface TreeNode {
  type: string;
  tagName?: string;
  value?: string;
  url?: string;
  properties?: { href?: unknown };
  children?: TreeNode[];
}

/** A link GitHub made from a bare URL or email address: its text is the address itself. */
function isAutolink(href: unknown, children: TreeNode[] | undefined): boolean {
  const [child, ...rest] = children ?? [];
  if (!child || rest.length > 0 || child.type !== "text" || typeof href !== "string") return false;
  const text = child.value ?? "";
  // Rendering percent-encodes a URL's `href` but not its text, as for `https://example.com/é`,
  // so both sides are encoded the same way, keeping escapes already written.
  const encodedHref = encodedUrl(href);
  return [text, `mailto:${text}`, `http://${text}`].some(
    (address) => href === address || (encodedHref !== null && encodedHref === encodedUrl(address)),
  );
}

/** `url` percent-encoded, with its existing `%XX` escapes kept; null if it can't be encoded. */
function encodedUrl(url: string): string | null {
  try {
    return url
      .split(/(%[0-9A-Fa-f]{2})/)
      .map((part, index) => (index % 2 === 1 ? part : encodeURI(part)))
      .join("");
  } catch {
    return null;
  }
}

/** HTML whose text GitHub shows as typed. */
const VERBATIM_TAGS = new Set(["code", "pre", "kbd", "samp", "script", "style", "textarea"]);
const HTML_TAG = /^<(\/?)([a-z][a-z0-9]*)\b[^>]*?(\/?)>$/i;

/** How an inline HTML node such as `<code>` or `</code>` changes the verbatim depth. */
function verbatimDepthChange(node: TreeNode): number {
  if (node.type !== "html") return 0;
  const tag = HTML_TAG.exec((node.value ?? "").trim());
  if (!tag || !VERBATIM_TAGS.has(tag[2]!.toLowerCase()) || tag[3]) return 0;
  return tag[1] ? -1 : 1;
}

/** The remark plugin, for plain-text excerpts: glyphs in place, picture emoji left as typed. */
export function remarkGitHubEmoji() {
  const visit = (node: TreeNode) => {
    if (node.type === "text" && node.value) {
      node.value = splitGitHubEmoji(node.value)
        .map((part) => ("text" in part ? part.text : `:${part.image}:`))
        .join("");
      return;
    }
    if (node.type === "link" && isAutolink(node.url, node.children)) return;
    // Text between inline HTML such as `<code>` and `</code>` stays as typed.
    let verbatimDepth = 0;
    for (const child of node.children ?? []) {
      verbatimDepth = Math.max(0, verbatimDepth + verbatimDepthChange(child));
      if (verbatimDepth === 0) visit(child);
    }
  };
  return (tree: TreeNode) => visit(tree);
}

// An image sized like the text around it, which shows its shortcode if it can't load.
function pictureEmoji(name: string): Element {
  const label = `:${name}:`;
  return {
    type: "element",
    tagName: "img",
    properties: {
      className: ["github-emoji-image"],
      src: githubImageEmojiUrl(name),
      alt: label,
      title: label,
      style: "width: 1.25em; height: 1.25em; vertical-align: -0.2em",
    },
    children: [],
  };
}

function replaceInChildren(children: HastContent[]): void {
  for (let index = 0; index < children.length; index += 1) {
    const child = children[index]!;
    if (child.type === "element") {
      if (VERBATIM_TAGS.has(child.tagName)) continue;
      if (child.tagName === "a" && isAutolink(child.properties.href, child.children)) continue;
      replaceInChildren(child.children);
      continue;
    }
    if (child.type !== "text") continue;
    const parts = splitGitHubEmoji(child.value);
    const replaced: ElementContent[] = parts.map((part) =>
      "text" in part ? { type: "text", value: part.text } : pictureEmoji(part.image),
    );
    children.splice(index, 1, ...replaced);
    index += replaced.length - 1;
  }
}

/**
 * The rehype plugin, for rendered Markdown: it runs on the HTML tree, so a shortcode in raw HTML
 * such as `<p>:hourglass:</p>` turns too. It adds its own markup, so it runs after sanitizing.
 */
export function rehypeGitHubEmoji() {
  return (tree: HastRoot) => replaceInChildren(tree.children);
}
