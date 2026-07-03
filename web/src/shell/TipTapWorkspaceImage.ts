// Workspace-aware TipTap Image extension.
//
// The stock @tiptap/extension-image renders `<img src>` directly, which only
// works for absolute URLs. Markdown files in a session workspace reference
// images by workspace-relative path (GitHub-style), and the filesystem API
// returns JSON (utf-8/base64) rather than raw bytes — so a bare <img> tag
// can't load them. This extension resolves relative srcs against the open
// file's directory, fetches the bytes through the authenticated filesystem
// endpoint, and displays them via a blob object URL.
//
// Serialisation safety: the node's `src` attr always keeps the ORIGINAL
// (relative) path — only the rendered DOM uses the blob URL — so
// getMarkdown() never leaks a blob/API URL into the file. (HTML-form
// images re-serialise with normalised attribute order/quoting, so the
// guarantee is attribute preservation, not whole-tag byte identity.)

import Image from "@tiptap/extension-image";
import Link from "@tiptap/extension-link";
import type { AnyExtension } from "@tiptap/core";
import { fetchFileContent, fileContentToBlob } from "@/hooks/useFileContent";

/**
 * Link extension that survives image-only links (GitHub badge pattern).
 *
 * Stock @tiptap/markdown applies marks to text nodes only — parsing
 * ``[![badge](img)](href)`` drops the link entirely because the link's only
 * child is an inline image atom. This extension attaches the link mark to
 * the image nodes directly in that case (the image node spec allows it, see
 * ``marks: "link"`` below). Mixed text+image link content falls through to
 * the stock behaviour.
 *
 * Use INSTEAD of the stock Link, with ``StarterKit.configure({ link: false })``
 * — StarterKit bundles its own Link in v3 and a duplicate would win the
 * markdown token registration.
 */
export const ImageAwareLink = Link.extend({
  parseMarkdown: (token, helpers) => {
    const attrs = { href: token.href, title: token.title || null };
    const content = helpers.parseInline(token.tokens || []);
    if (content.length > 0 && content.every((node) => node.type === "image")) {
      return content.map((node) => ({
        ...node,
        marks: [...(node.marks ?? []), { type: "link", attrs }],
      }));
    }
    return helpers.applyMark("link", content, attrs);
  },
});

/**
 * Whether an image src points into the session workspace (vs an external URL).
 *
 * External: any scheme (`http:`, `https:`, `data:`, …) or protocol-relative
 * (`//host/…`). Everything else — `docs/x.png`, `./x.png`, `/docs/x.png` —
 * is treated as a workspace path.
 *
 * :param src: The raw src from the markdown/HTML, e.g. ``"docs/logo.svg"``.
 * :returns: True when the src should be fetched from the workspace filesystem.
 */
export function isWorkspaceRelativeSrc(src: string): boolean {
  return !/^([a-z][a-z0-9+.-]*:|\/\/)/i.test(src);
}

/**
 * Resolve a workspace-relative image src against the open file's location.
 *
 * Mirrors GitHub's resolution rules: a leading ``/`` is relative to the
 * workspace root, anything else is relative to the file's directory.
 * ``.``/``..`` segments are normalised (``..`` clamps at the workspace
 * root), query/fragment suffixes are dropped, and percent-escapes are
 * decoded since the filesystem API re-encodes each segment itself.
 *
 * :param filePath: Workspace-relative path of the open markdown file,
 *     e.g. ``"docs/README.md"``.
 * :param src: The image src as written in the file, e.g. ``"../img/x.png"``.
 * :returns: Normalised workspace-relative path, e.g. ``"img/x.png"``.
 */
export function resolveWorkspacePath(filePath: string, src: string): string {
  const cleanSrc = src.split(/[?#]/)[0];
  const segments = cleanSrc.startsWith("/") ? [] : filePath.split("/").slice(0, -1);
  for (const rawSegment of cleanSrc.split("/")) {
    let segment = rawSegment;
    try {
      const decoded = decodeURIComponent(rawSegment);
      // A decoded "/" or "\" would change the path structure downstream
      // (fetchFileContent re-splits on "/"), letting %2F smuggle extra
      // segments past the normalisation here. Keep such segments verbatim.
      if (!decoded.includes("/") && !decoded.includes("\\")) {
        segment = decoded;
      }
    } catch {
      // Malformed escape in user-authored markdown — use the segment verbatim.
    }
    if (segment === "" || segment === ".") continue;
    // `..` above the workspace root pops an empty array (no-op), clamping at root.
    if (segment === "..") segments.pop();
    else segments.push(segment);
  }
  return segments.join("/");
}

/**
 * Minimal HTML attribute-value escaping for serialised ``<img>`` tags.
 *
 * :param value: Raw attribute value, e.g. an image alt text.
 * :returns: Value safe to embed inside a double-quoted HTML attribute.
 */
function escapeHtmlAttr(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

/**
 * Escape characters that would terminate markdown link/image label text.
 *
 * A ``]`` (or a stray ``\``) inside alt text would close the ``![…]`` label
 * early on re-parse, corrupting the document. The source file must itself
 * have written these escaped, so escaping here reproduces the original bytes.
 *
 * :param value: The label text, e.g. an image alt like ``"a]b"``.
 * :returns: Text safe inside ``![…]`` / ``[…]``, e.g. ``"a\\]b"``.
 */
function escapeMarkdownLabel(value: string): string {
  return value.replace(/([\\[\]])/g, "\\$1");
}

/**
 * Format a markdown link/image destination (the ``(…)`` part).
 *
 * CommonMark bare destinations cannot contain whitespace, ``<``/``>``, or
 * unbalanced parentheses — those need the angle-bracket form ``<dest>``.
 * Balanced parens are legal bare, so e.g. ``x(1).png`` stays unwrapped to
 * keep serialisation byte-faithful for files that wrote it bare.
 *
 * :param dest: The raw destination, e.g. ``"my file.png"``.
 * :returns: The destination as it must appear in markdown,
 *     e.g. ``"<my file.png>"``.
 */
function formatMarkdownDestination(dest: string): string {
  let needsBrackets = /[\s<>]/.test(dest);
  let depth = 0;
  for (const ch of dest) {
    if (ch === "(") depth += 1;
    else if (ch === ")") depth -= 1;
    if (depth < 0) break;
  }
  needsBrackets ||= depth !== 0;
  return needsBrackets ? `<${dest.replace(/([\\<>])/g, "\\$1")}>` : dest;
}

/**
 * Format an optional markdown title as a destination suffix.
 *
 * :param title: The title text, or null/empty when absent.
 * :returns: ``' "escaped title"'`` (leading space included) or ``""``,
 *     ready to append inside a ``(…)`` destination.
 */
function formatMarkdownTitleSuffix(title: string | null | undefined): string {
  // Unescaped " would close the title early on re-parse.
  return title ? ` "${title.replace(/([\\"])/g, "\\$1")}"` : "";
}

/**
 * Build the Image extension bound to a session workspace file.
 *
 * Created per editor mount (the viewer remounts when the path changes, so
 * the closed-over identifiers stay correct for the editor's lifetime).
 *
 * :param conversationId: The session/conversation ID, e.g. ``"conv_abc123"``.
 * :param filePath: Workspace-relative path of the file being viewed,
 *     e.g. ``"README.md"``.
 * :returns: A configured TipTap extension to include in the editor's list.
 */
export function createWorkspaceImageExtension(
  conversationId: string,
  filePath: string,
): AnyExtension {
  return Image.extend({
    // ProseMirror disallows all marks on inline atoms by default, which
    // strips the link off `[![badge](img)](href)` at parse time. Allow link
    // so GitHub-style linked badges survive the round-trip.
    marks: "link",
    addAttributes() {
      return {
        ...this.parent?.(),
        // Presentational attrs GitHub READMEs commonly put on raw <img>
        // tags. Carried through the schema so a save doesn't strip them.
        valign: { default: null },
        align: { default: null },
      };
    },
    // Markdown can't express width/height, so sized images (parsed from HTML
    // <img> tags) serialise back to HTML to preserve their dimensions; plain
    // images keep the stock `![alt](src)` form.
    renderMarkdown(node) {
      // ?? "" matches the stock extension's serialisation contract: markdown
      // image syntax has no "absent" form — `![](src)` IS the empty alt/title.
      const src: string = node.attrs?.src ?? "";
      const alt: string = node.attrs?.alt ?? "";
      const title: string = node.attrs?.title ?? "";
      const width = node.attrs?.width;
      const height = node.attrs?.height;
      const valign = node.attrs?.valign;
      const align = node.attrs?.align;
      const needsHtmlForm = width != null || height != null || valign != null || align != null;
      let image: string;
      if (needsHtmlForm) {
        const attrs = [`src="${escapeHtmlAttr(src)}"`];
        // `alt != null` (not truthiness): an explicit alt="" on the source
        // tag must be preserved, only an absent alt is omitted.
        if (node.attrs?.alt != null) attrs.push(`alt="${escapeHtmlAttr(alt)}"`);
        if (title) attrs.push(`title="${escapeHtmlAttr(title)}"`);
        if (width != null) attrs.push(`width="${escapeHtmlAttr(String(width))}"`);
        if (height != null) attrs.push(`height="${escapeHtmlAttr(String(height))}"`);
        if (valign != null) attrs.push(`valign="${escapeHtmlAttr(String(valign))}"`);
        if (align != null) attrs.push(`align="${escapeHtmlAttr(String(align))}"`);
        image = `<img ${attrs.join(" ")} />`;
      } else {
        const label = escapeMarkdownLabel(alt);
        const dest = formatMarkdownDestination(src);
        image = `![${label}](${dest}${formatMarkdownTitleSuffix(title)})`;
      }
      // The serialiser's mark open/close tracking is text-node-centric and
      // never emits marks around inline atoms, so a link mark (badge pattern,
      // attached by ImageAwareLink) must be wrapped here.
      const linkMark = (node.marks ?? []).find(
        (mark) => (typeof mark === "string" ? mark : mark.type) === "link",
      );
      const href = typeof linkMark === "object" ? linkMark.attrs?.href : undefined;
      if (!href) return image;
      // The link mark's title must survive too: `[![b](i)](href "t")`
      // parsed by ImageAwareLink stores it in attrs, and dropping it here
      // would rewrite the file on save.
      const linkTitle = typeof linkMark === "object" ? linkMark.attrs?.title : undefined;
      const linkDest = formatMarkdownDestination(String(href));
      return `[${image}](${linkDest}${formatMarkdownTitleSuffix(linkTitle as string | null)})`;
    },
    addNodeView() {
      return ({ node, HTMLAttributes }) => {
        const img = document.createElement("img");
        for (const [key, value] of Object.entries(HTMLAttributes)) {
          // src is handled below: workspace paths must go through the
          // authenticated fetch, never directly onto the DOM.
          if (key === "src" || value == null) continue;
          img.setAttribute(key, String(value));
        }
        const src: string = node.attrs.src ?? "";
        let cancelled = false;
        let objectUrl: string | null = null;
        if (src === "") {
          // No src (e.g. `![]()`): leave the img srcless — the alt text
          // renders as the fallback. Fetching would request the file's
          // own directory, a guaranteed-failing call.
        } else if (isWorkspaceRelativeSrc(src)) {
          // Srcs with no path component (`![](#frag)`, `![](?raw=true)`) or
          // that resolve to the workspace root (`![](/)`) never name a
          // fetchable file — resolving would target a directory or "".
          // Treat them like an empty src (alt-text fallback, no request).
          const pathPart = src.split(/[?#]/)[0];
          const resolved = pathPart === "" ? "" : resolveWorkspacePath(filePath, src);
          if (resolved !== "") {
            fetchFileContent(conversationId, resolved)
              .then((data) => {
                if (cancelled) return;
                objectUrl = URL.createObjectURL(fileContentToBlob(data));
                img.src = objectUrl;
              })
              .catch(() => {
                // Missing/unreadable file: leave src unset so the browser
                // shows the alt text as the standard broken-image fallback.
              });
          }
        } else {
          img.src = src;
        }
        return {
          dom: img,
          // Without update(), ProseMirror recreates the node view whenever
          // the containing block is redrawn — typing next to an inline image
          // would refetch it and mint a new blob URL on every keystroke.
          // Keep the element (and its blob URL) while src is unchanged; a
          // src change returns false so the view is destroyed (revoking the
          // old URL) and recreated, refetching through the path above.
          update(newNode) {
            if (newNode.type !== node.type || newNode.attrs.src !== src) return false;
            for (const [key, value] of Object.entries(newNode.attrs)) {
              if (key === "src") continue;
              if (value == null) img.removeAttribute(key);
              else img.setAttribute(key, String(value));
            }
            node = newNode;
            return true;
          },
          destroy() {
            cancelled = true;
            if (objectUrl) URL.revokeObjectURL(objectUrl);
          },
        };
      };
    },
    // inline matches GitHub: badges and logos sit inside headings/paragraphs.
  }).configure({ inline: true });
}
