// Markdown rendering for chat text: assistant bubbles, user bubbles, and any
// other surface that shows agent-authored markdown.
//
// Two affordances beyond plain markdown, both routing to the FileViewer so a
// file the agent names is one click away:
//
//   1. Inline-code paths (`` `src/App.tsx` ``): see `WorkspacePathInlineCode`.
//   2. Markdown links whose href is a file path: see `WorkspaceFileLink`.
//
// Lives apart from `BlockRenderer` so approval cards can render agent markdown
// without importing the block dispatcher that renders them.

import type React from "react";
import { useMemo } from "react";
import { defaultRemarkPlugins } from "streamdown";
import remarkBreaks from "remark-breaks";
import { normalizeExplicitMathDelimiters } from "@/components/ai-elements/mathMarkdown";
import { MessageResponse } from "@/components/ai-elements/message";
import { WORKSPACE_FILE_LINK_ATTR } from "@/components/ai-elements/streamdown-security";
import { ZoomableImage } from "@/components/ImageLightbox";
import { useThrottledValue } from "@/hooks/useThrottledValue";
import { isElectronShell } from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";
import {
  useFileViewer,
  useFileViewerConversationId,
  useIsChangedPath,
  useWorkspacePaths,
} from "@/shell/FileViewerContext";
import { toWorkspaceRelativePath, useWorkspaceFileExists } from "@/hooks/useWorkspaceChangedFiles";

// Streamdown hands each component override the source hast node alongside the
// element's own props. Overrides here spread their props onto a DOM element, so
// they destructure `node` away first. Left in, it renders as a literal
// node="[object Object]" attribute.
type WithHastNode<T> = T & { node?: unknown };

// Trailing `:line` / `:line:col` on a cited path, e.g. `src/app.ts:42:7`.
const POSITION_SUFFIX = /:\d+(?::\d+)?$/;

/**
 * Resolves `text` to an openable workspace file, returning the click handler
 * that opens it in the FileViewer, or null when it isn't one (no FileViewer,
 * unresolvable path, or no such file). Shared by the inline-code and link
 * renderers so both judge a path the same way.
 */
function useWorkspaceFileOpener(text: string): (() => void) | null {
  const openFile = useFileViewer();
  const isChangedPath = useIsChangedPath();
  const conversationId = useFileViewerConversationId();
  const { root, home } = useWorkspacePaths();

  // Agents cite a file with the position they mean, `docs/notes.md:12` or
  // `:12:7`. The position is not part of the filename, so no such path is ever
  // in the changed-files list or on disk; drop it before resolving. The span
  // still displays the citation the agent wrote.
  const cited = text.replace(POSITION_SUFFIX, "");
  // Collapse absolute / "~"-relative forms onto a workspace-relative path so
  // they match the changed-files list and the filesystem API. null = absolute
  // or "~" path outside the workspace (or the root itself) → never a link.
  const linkPath = cited ? toWorkspaceRelativePath(cited, root, home) : null;
  // "Trusted" means we resolved an absolute/"~" form against the root, so the
  // result is known workspace-relative even if it's a bare basename (no
  // interior slash) that the existence check's path-shape heuristic rejects.
  const trusted = linkPath !== null && linkPath !== cited;

  const isChanged = !!linkPath && isChangedPath(linkPath);
  // Only hit the filesystem for path-shaped spans that aren't already known
  // changes; passing null disables the query (keeps hook order stable).
  const existsOnDisk = useWorkspaceFileExists(
    conversationId,
    openFile && linkPath && !isChanged ? linkPath : null,
    trusted,
  );

  if (!openFile || !linkPath || !(isChanged || existsOnDisk)) return null;
  return () => openFile(linkPath);
}

/**
 * Inline-`code` renderer that turns workspace file paths (e.g.
 * `` `src/components/App.tsx` ``) into clickable links opening the FileViewer.
 *
 * The span's text is first collapsed to a workspace-relative path: an
 * absolute (`/home/u/ws/foo.md`) or home-relative (`~/ws/foo.md`) path under
 * the workspace root is stripped down to its relative form so it matches the
 * changed-files list and the filesystem API (both speak relative paths);
 * absolute/`~` paths outside the root resolve to null and never linkify.
 *
 * That relative path is then linkified when it is either (a) a known
 * agent-changed file — resolved synchronously, the fast path, and the only
 * path that may be an uncommitted/deleted file — or (b) a path-shaped string
 * that the filesystem API confirms points at a real file in the workspace.
 * Everything else (prose-y inline code, non-existent paths) falls back to a
 * styled `<code>` matching Streamdown's default inline appearance. The span
 * always *displays* the original text the agent wrote; only the link target
 * uses the resolved relative path.
 *
 * Rendered by Streamdown as a real component (via the `inlineCode` slot), so
 * it may call hooks: the existence query re-renders this span when it settles,
 * independent of whether `MessageResponse` re-renders its parent.
 */
function WorkspacePathInlineCode({
  children: codeChildren,
  className,
  node: _node,
  ...codeProps
}: WithHastNode<React.ComponentPropsWithoutRef<"code">>) {
  const text = typeof codeChildren === "string" ? codeChildren : "";
  const openWorkspaceFile = useWorkspaceFileOpener(text);

  if (openWorkspaceFile) {
    // Rendered as an inline <code> (not a <button>): a button is laid out as
    // an atomic inline-block, so a long path can't break across lines and
    // drops below the list marker as a whole unit. An inline <code> flows and
    // wraps like the surrounding text; role/tabIndex/keydown restore the
    // button semantics.
    return (
      <code
        role="button"
        tabIndex={0}
        data-streamdown="inline-code"
        // Keep the base inline-code class/props (merge, don't replace) so the
        // link only adds the underline affordance on top of Streamdown's
        // styling and any caller-provided attributes survive.
        className={cn(
          "font-mono text-ui underline decoration-dotted underline-offset-2 hover:text-foreground transition-colors cursor-pointer",
          className,
        )}
        onClick={openWorkspaceFile}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            openWorkspaceFile();
          }
        }}
        {...codeProps}
      >
        {codeChildren}
      </code>
    );
  }
  // Match Streamdown's default inline-code styling so non-path inline code
  // looks unchanged.
  return (
    <code
      className={cn("rounded bg-muted px-1.5 py-0.5 font-mono text-ui", className)}
      data-streamdown="inline-code"
      {...codeProps}
    >
      {codeChildren}
    </code>
  );
}

// Streamdown's own anchor styling and marker attribute. Overriding the `a`
// slot replaces its link component wholesale, so both must be reproduced here:
// index.css keys the pointer cursor and the table-cell `overflow-wrap` rule
// (which stops a link-only table column collapsing to ~2ch) on the attribute.
const STREAMDOWN_LINK_CLASS = "wrap-anywhere font-medium text-primary underline";

/**
 * Anchor renderer for markdown links. A link to a workspace file, its href
 * parked on a fragment by `markWorkspaceFileLinks` and the real path moved to
 * `WORKSPACE_FILE_LINK_ATTR`, opens the FileViewer instead of navigating,
 * matching how an inline-code path behaves. Every other link (http(s), mailto,
 * in-page anchors) still carries its own href and renders as Streamdown would.
 *
 * A marked path that names no workspace file renders as plain text: its href is
 * the parked fragment, so a live link there would go nowhere.
 */
function WorkspaceFileLink({
  href,
  children,
  className,
  title,
  target: _target,
  rel: _rel,
  node: _node,
  ...props
}: WithHastNode<React.ComponentPropsWithoutRef<"a">>) {
  const marked = (props as Record<string, unknown>)[WORKSPACE_FILE_LINK_ATTR];
  const path = typeof marked === "string" ? marked : "";
  const openWorkspaceFile = useWorkspaceFileOpener(path);

  if (!path) {
    const opensOutsideShell = isElectronShell();
    return (
      <a
        {...props}
        href={href}
        target={opensOutsideShell ? "_blank" : "_self"}
        rel={opensOutsideShell ? "noopener noreferrer" : "noreferrer"}
        className={cn(STREAMDOWN_LINK_CLASS, className)}
        title={title}
        data-streamdown="link"
      >
        {children}
      </a>
    );
  }

  // Marked but not an openable file. Drop to text rather than leave a link on
  // the parked fragment, keeping the path on hover so it stays discoverable.
  if (!openWorkspaceFile) {
    return (
      <span className={className} title={title ?? path}>
        {children}
      </span>
    );
  }

  // No href: the parked fragment would still navigate on cmd/middle-click, and
  // no URL opens the FileViewer. role/tabIndex/onKeyDown restore button semantics.
  return (
    <a
      {...props}
      role="button"
      tabIndex={0}
      title={title ?? path}
      data-streamdown="link"
      // Dotted underline distinguishes "opens in the FileViewer" from a link
      // that leaves the app; the rest matches Streamdown so a file link in a
      // table cell wraps like any other.
      className={cn(STREAMDOWN_LINK_CLASS, "decoration-dotted underline-offset-2", className)}
      onClick={openWorkspaceFile}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openWorkspaceFile();
        }
      }}
    >
      {children}
    </a>
  );
}

// Markdown images open in the shared lightbox on click, matching uploaded and
// generated images. (Remote `src`s are still gated by Streamdown's image
// security; this only adds the zoom affordance to whatever does render.)
function ZoomableMarkdownImage({
  src,
  alt,
  node: _node,
  ...props
}: WithHastNode<React.ComponentProps<"img">>) {
  const resolvedSrc = typeof src === "string" ? src : undefined;
  return <ZoomableImage {...props} src={resolvedSrc} alt={alt ?? ""} />;
}

// Stable module-level override map so MessageResponse's shallow prop compare
// never sees a new `components` identity and re-parses needlessly.
const FILE_PATH_AWARE_COMPONENTS = {
  inlineCode: WorkspacePathInlineCode,
  img: ZoomableMarkdownImage,
  a: WorkspaceFileLink,
};

// How often the live (growing) assistant bubble re-parses its markdown. The
// store pump commits a new, longer text up to once per animation frame (~60/s);
// without this the whole accumulated message is re-parsed on every commit. ~10/s
// is smooth to read and cuts the per-frame parse cost. Trailing-edge, so the
// final text still appears within this window of the last token.
const STREAM_MARKDOWN_THROTTLE_MS = 100;

// Defense-in-depth against a pathological text block locking the tab.
// A user message whose text is a ~50KB unbroken base64 data URL
// — e.g. an image block accidentally serialized into the text stream — both
// jams the full markdown pipeline (Shiki/KaTeX/mermaid + rehype) on the main
// thread AND forces the browser to lay out one ~50K-char line with no break
// opportunities. Either heuristic below routes such a block to plain,
// break-anywhere rendering that bypasses markdown entirely.
//
// `MAX_MARKDOWN_TEXT_LENGTH`: total size above which we never run markdown.
// `MAX_UNBROKEN_TOKEN_LENGTH`: longest run of non-whitespace chars above which
//   layout becomes pathological regardless of total size (base64, long URLs).
// `MAX_PLAINTEXT_DISPLAY_LENGTH`: hard cap on what we paint even as plain text,
//   so a multi-MB payload can't blow up the DOM; the rest is elided.
const MAX_MARKDOWN_TEXT_LENGTH = 50_000;
const MAX_UNBROKEN_TOKEN_LENGTH = 5_000;
const MAX_PLAINTEXT_DISPLAY_LENGTH = 200_000;

/**
 * Longest run of consecutive non-whitespace characters in `text`. ASCII
 * whitespace (space, tab, CR, LF, FF, VT) resets the run — those are the
 * break opportunities the layout engine can use. O(n), single pass.
 */
function longestUnbrokenRun(text: string): number {
  let max = 0;
  let current = 0;
  for (let i = 0; i < text.length; i += 1) {
    const code = text.charCodeAt(i);
    // 32 = space; 9..13 = tab, LF, VT, FF, CR.
    if (code === 32 || (code >= 9 && code <= 13)) {
      current = 0;
    } else {
      current += 1;
      if (current > max) max = current;
    }
  }
  return max;
}

/**
 * Whether `text` should bypass the markdown pipeline because rendering it
 * there would risk locking the tab. See the constants above for the why.
 */
function isPathologicalText(text: string): boolean {
  return (
    text.length > MAX_MARKDOWN_TEXT_LENGTH || longestUnbrokenRun(text) > MAX_UNBROKEN_TOKEN_LENGTH
  );
}

/**
 * Plain, break-anywhere fallback for a pathological text block — no markdown.
 * `whitespace-pre-wrap` keeps newlines; `break-all` gives the layout engine a
 * break opportunity inside an otherwise unbreakable token. Over-long payloads
 * are elided so the DOM node itself can't grow without bound.
 */
function PlainTextFallback({ text }: { text: string }) {
  const truncated = text.length > MAX_PLAINTEXT_DISPLAY_LENGTH;
  const shown = truncated ? text.slice(0, MAX_PLAINTEXT_DISPLAY_LENGTH) : text;
  return (
    <div className="whitespace-pre-wrap break-all font-mono text-sm">
      {shown}
      {truncated && (
        <span className="text-muted-foreground">
          {`\n… [${text.length - MAX_PLAINTEXT_DISPLAY_LENGTH} more characters not shown]`}
        </span>
      )}
    </div>
  );
}

/**
 * Wraps `MessageResponse` with {@link WorkspacePathInlineCode} via Streamdown's
 * `inlineCode` slot — NOT `code` — so fenced code blocks keep their default
 * `<pre>` wrapper and Shiki highlighting. Overriding `code` here would replace
 * block rendering too, stripping `<pre>` and collapsing whitespace.
 *
 * When `breaks` is set, single newlines render as `<br>` (remark-breaks)
 * instead of collapsing to spaces per CommonMark. Used for user bubbles,
 * where people type multi-line messages without blank-line paragraph
 * separators and expect their line breaks preserved. NOTE: Streamdown's
 * `remarkPlugins` prop *replaces* its defaults rather than merging, so we
 * extend `defaultRemarkPlugins` (which carries remark-gfm) — passing
 * `[remarkBreaks]` alone would silently drop GFM tables / strikethrough.
 */
export function FilePathAwareMessageResponse({
  children,
  breaks = false,
  ...props
}: React.ComponentProps<typeof MessageResponse> & { breaks?: boolean }) {
  const components = FILE_PATH_AWARE_COMPONENTS;

  // Extend (don't replace) Streamdown's defaults so remark-gfm survives;
  // append remark-breaks only when `breaks` is requested. When `breaks` is
  // false we pass `undefined` so Streamdown uses its own defaults unchanged.
  const remarkPlugins = useMemo(
    () => (breaks ? [...Object.values(defaultRemarkPlugins), remarkBreaks] : undefined),
    [breaks],
  );

  // Throttle the markdown so the live (still-growing) bubble re-parses a few
  // times per second instead of on every store commit. `children` is a string
  // at both call sites (a text RenderItem and the user bubble); finalized/static
  // text changes once, which emits immediately, so this is a no-op off the
  // streaming path. The hook must be called unconditionally (rules of hooks), so
  // non-string children (none today) pass an inert "" and bypass the result.
  const isString = typeof children === "string";
  const normalizedText = useMemo(
    () => (isString ? normalizeExplicitMathDelimiters(children as string) : ""),
    [isString, children],
  );
  const throttledText = useThrottledValue(normalizedText, STREAM_MARKDOWN_THROTTLE_MS);

  // Defense-in-depth: a string child that is huge or carries a
  // giant unbroken token (e.g. a base64 data URL serialized into the text
  // stream) would lock the tab in the markdown pipeline + layout. Render it as
  // plain break-anywhere text instead. Both call sites (assistant text blocks
  // and the user bubble) flow through here, so this one guard covers both.
  const pathological = useMemo(
    () => isString && isPathologicalText(children as string),
    [isString, children],
  );
  if (pathological) {
    return <PlainTextFallback text={children as string} />;
  }

  return (
    <MessageResponse {...props} components={components} remarkPlugins={remarkPlugins} markFileLinks>
      {isString ? throttledText : children}
    </MessageResponse>
  );
}
