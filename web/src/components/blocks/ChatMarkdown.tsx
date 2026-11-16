// Markdown rendering for chat text: assistant bubbles, user bubbles, and any
// other surface that shows agent-authored markdown.
//
// File and folder references the agent writes (markdown links, inline code,
// bare prose paths) open the FileViewer or Files tree: see `ChatFileReference`.
//
// Lives apart from `BlockRenderer` so approval cards can render agent markdown
// without importing the block dispatcher that renders them.

import type React from "react";
import { useInRouterContext } from "react-router-dom";
import { Link } from "@/lib/routing";
import { useMemo } from "react";
import {
  BotIcon,
  FileTextIcon,
  FolderIcon,
  GitPullRequestIcon,
  MessageSquareIcon,
} from "lucide-react";
import { defaultRemarkPlugins } from "streamdown";
import remarkBreaks from "remark-breaks";
import { normalizeExplicitMathDelimiters } from "@/components/ai-elements/mathMarkdown";
import { MessageResponse } from "@/components/ai-elements/message";
import {
  PROSE_FILE_REFERENCE_TAG,
  sameServerSessionPath,
  WORKSPACE_FILE_LINK_ATTR,
  WORKSPACE_IMAGE_ATTR,
} from "@/components/ai-elements/streamdown-security";
import {
  InlineCodeFileReference,
  MarkdownFileLink,
  ProseFileReference,
  STREAMDOWN_LINK_CLASS,
  useMentionedFile,
  type WithHastNode,
} from "./ChatFileReference";
import { WorkspaceMarkdownImage } from "./WorkspaceMarkdownImage";
import { ZoomableImage } from "@/components/ImageLightbox";
import { VideoPlayer } from "@/components/VideoPlayer";
import { isRemoteVideoUrl } from "@/lib/video";
import { useThrottledValue } from "@/hooks/useThrottledValue";
import { withBasePath } from "@/lib/basePath";
import { followLinkWithPopupFallback } from "@/lib/openLinkInApp";
import { cn } from "@/lib/utils";
import { parseMentionPillTitle } from "@/lib/mentionPills";
import { useFileViewerConversationId } from "@/shell/FileViewerContext";

function githubPullRequest(href: string | undefined): string | null {
  if (!href) return null;
  try {
    const url = new URL(href);
    if (url.protocol !== "https:" || url.hostname !== "github.com") return null;
    const match = url.pathname.match(/^\/[^/]+\/([^/]+)\/pull\/(\d+)\/?$/);
    return match ? `${match[1]}#${match[2]}` : null;
  } catch {
    return null;
  }
}

function PullRequestLinkLabel({ children }: { children: React.ReactNode }) {
  const words = typeof children === "string" ? children.match(/^(\S+)([\s\S]*)$/) : null;
  const prefix = (
    <>
      <GitPullRequestIcon aria-hidden="true" className="inline-block size-[1em] align-[-0.125em]" />
      {"\u00a0"}
      {words?.[1]}
    </>
  );
  return (
    <>
      {words ? <span className="whitespace-nowrap">{prefix}</span> : prefix}
      {words ? words[2] : children}
    </>
  );
}

/**
 * The in-app chat path for an `omnigent://<host>/c/<id>` link to the server this
 * page is on, or null. Links to other servers keep their deep-link href.
 */
function currentServerSessionPath(href: unknown): string | null {
  if (typeof href !== "string" || typeof window === "undefined") return null;
  return sameServerSessionPath(href, window.location);
}

/** A session link to this server, routed in the app like a session mention pill. */
function ServerSessionLink({
  path,
  className,
  children,
  target: _target,
  rel: _rel,
  ...props
}: React.ComponentPropsWithoutRef<"a"> & { path: string }) {
  const linkClass = cn(STREAMDOWN_LINK_CLASS, className);
  // Markdown also renders outside the router (stories, embeds); there the href navigates.
  return useInRouterContext() ? (
    <Link to={path} className={linkClass} data-streamdown="link" {...props}>
      {children}
    </Link>
  ) : (
    <a href={withBasePath(path)} className={linkClass} data-streamdown="link" {...props}>
      {children}
    </a>
  );
}

/**
 * Anchor renderer for markdown links. A link to a local path, its href parked
 * by `markWorkspaceFileLinks` and the path moved to `WORKSPACE_FILE_LINK_ATTR`,
 * renders as a {@link MarkdownFileLink}. Every other link (http(s), mailto,
 * in-page anchors, session links) still carries its own href.
 */
function ChatLink({
  href,
  children,
  className,
  title,
  node: _node,
  ...props
}: WithHastNode<React.ComponentPropsWithoutRef<"a">>) {
  const marked = (props as Record<string, unknown>)[WORKSPACE_FILE_LINK_ATTR];
  // Scopes the desktop in-app browser route to this conversation's view.
  const conversationId = useFileViewerConversationId();

  if (typeof marked === "string" && marked) {
    const { [WORKSPACE_FILE_LINK_ATTR]: _path, ...rest } = props as Record<string, unknown>;
    return (
      <MarkdownFileLink
        path={marked}
        className={className}
        title={title}
        {...(rest as React.ComponentPropsWithoutRef<"a">)}
      >
        {children}
      </MarkdownFileLink>
    );
  }
  if (typeof href === "string" && isRemoteVideoUrl(href)) {
    return (
      <VideoPlayer
        compact
        src={href}
        title={typeof children === "string" ? children : "Video recording"}
      />
    );
  }
  const sessionPath = currentServerSessionPath(href);
  if (sessionPath) {
    return (
      <ServerSessionLink path={sessionPath} className={className} title={title} {...props}>
        {children}
      </ServerSessionLink>
    );
  }
  // Rebase an app-internal link (e.g. an agent's `/clear` "the new chat"
  // `/c/<id>`) under the deployment base path; no-op for external URLs, `#`
  // fragments, and at the origin root. The router basename does not reach raw
  // markdown anchors, so they are prefixed here.
  const rebased = typeof href === "string" ? withBasePath(href) : href;
  // Streamdown renders external links with target="_blank"; those need the
  // popup fallback so a click still works where new tabs can't open.
  const blankHref = props.target === "_blank" && typeof rebased === "string" ? rebased : null;
  const pullRequest = githubPullRequest(rebased);
  return (
    <a
      href={rebased}
      className={cn(STREAMDOWN_LINK_CLASS, className)}
      title={title ?? (pullRequest ? rebased : undefined)}
      data-streamdown="link"
      {...props}
      onClick={
        blankHref === null
          ? props.onClick
          : (event) => {
              props.onClick?.(event);
              followLinkWithPopupFallback(event, blankHref, conversationId);
            }
      }
    >
      {pullRequest ? (
        <PullRequestLinkLabel>{children === href ? pullRequest : children}</PullRequestLinkLabel>
      ) : (
        children
      )}
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
  [WORKSPACE_IMAGE_ATTR]: workspacePath,
  ...props
}: WithHastNode<React.ComponentProps<"img">> & { [WORKSPACE_IMAGE_ATTR]?: string }) {
  if (workspacePath) {
    return <WorkspaceMarkdownImage path={workspacePath} alt={alt ?? ""} />;
  }
  const resolvedSrc = typeof src === "string" ? src : undefined;
  return <ZoomableImage {...props} src={resolvedSrc} alt={alt ?? ""} />;
}

// Stable module-level override map so MessageResponse's shallow prop compare
// never sees a new `components` identity and re-parses needlessly.
/** Same pink as the composer's mention tokens, so a tag looks alike before and after send. */
const MENTION_PILL_CLASS =
  "inline-flex max-w-full items-center gap-1 rounded-[4px] bg-brand-accent/10 px-1 align-baseline text-[0.95em] leading-snug text-brand-accent no-underline ring-2 ring-brand-accent/10 hover:bg-brand-accent/20";

/** A tagged file in a sent message; opens it like a workspace file link. */
function FileMentionPill({
  path,
  isDir,
  lineSpan,
  children,
}: {
  path: string;
  isDir: boolean;
  lineSpan?: string;
  children: React.ReactNode;
}) {
  const open = useMentionedFile(path);
  const Icon = isDir ? FolderIcon : FileTextIcon;
  return (
    <button
      type="button"
      data-testid="mention-pill"
      className={MENTION_PILL_CLASS}
      // Disabled until the existence check says how to open the path.
      aria-disabled={open ? undefined : true}
      // The label can truncate; the hover title keeps the full path and line span.
      title={lineSpan ? `${path}:${lineSpan}` : path}
      onClick={(event) => {
        event.stopPropagation();
        open?.();
      }}
    >
      <Icon className="size-3 shrink-0" />
      <span className="truncate">{children}</span>
    </button>
  );
}

/** Links rendered as mention pills when their title carries the mention sentinel. */
function MarkdownLink(props: WithHastNode<React.ComponentPropsWithoutRef<"a">>) {
  const target = parseMentionPillTitle(props.title);
  if (!target) return <ChatLink {...props} />;
  if (target.kind === "file") {
    return (
      <FileMentionPill path={target.path} isDir={target.isDir} lineSpan={target.lineSpan}>
        {props.children}
      </FileMentionPill>
    );
  }
  return (
    <SessionMentionPill id={target.id} isSubagent={target.isSubagent}>
      {props.children}
    </SessionMentionPill>
  );
}

interface SessionMentionPillProps {
  id: string;
  isSubagent: boolean;
  children: React.ReactNode;
}

/** A session pill (e.g. ``@Fix CI``) that opens session ``id`` without reloading the page. */
export function SessionMentionPill(props: SessionMentionPillProps) {
  // Markdown also renders outside the router (stories, embeds); there the href navigates.
  return useInRouterContext() ? (
    <RoutedSessionMentionPill {...props} />
  ) : (
    <SessionPillLink {...props} />
  );
}

// The routing seam's Link rebases the href under an embed's mount, routes plain
// clicks in-app and leaves modified clicks (new tab) to the browser.
function RoutedSessionMentionPill({ id, isSubagent, children }: SessionMentionPillProps) {
  return (
    <Link
      to={`/c/${encodeURIComponent(id)}`}
      data-testid="mention-pill"
      className={MENTION_PILL_CLASS}
      title={isSubagent ? "Subagent" : "Session"}
      onClick={(event) => event.stopPropagation()}
    >
      <SessionPillContent isSubagent={isSubagent}>{children}</SessionPillContent>
    </Link>
  );
}

function SessionPillLink({ id, isSubagent, children }: SessionMentionPillProps) {
  return (
    <a
      data-testid="mention-pill"
      href={withBasePath(`/c/${encodeURIComponent(id)}`)}
      className={MENTION_PILL_CLASS}
      title={isSubagent ? "Subagent" : "Session"}
      onClick={(event) => event.stopPropagation()}
    >
      <SessionPillContent isSubagent={isSubagent}>{children}</SessionPillContent>
    </a>
  );
}

function SessionPillContent({
  isSubagent,
  children,
}: {
  isSubagent: boolean;
  children: React.ReactNode;
}) {
  const Icon = isSubagent ? BotIcon : MessageSquareIcon;
  return (
    <>
      <Icon className="size-3 shrink-0" />
      <span className="truncate">{children}</span>
    </>
  );
}

const FILE_PATH_AWARE_COMPONENTS = {
  inlineCode: InlineCodeFileReference,
  [PROSE_FILE_REFERENCE_TAG]: ProseFileReference,
  img: ZoomableMarkdownImage,
  a: MarkdownLink,
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
function PlainTextFallback({ text, className }: { text: string; className?: string }) {
  const truncated = text.length > MAX_PLAINTEXT_DISPLAY_LENGTH;
  const shown = truncated ? text.slice(0, MAX_PLAINTEXT_DISPLAY_LENGTH) : text;
  return (
    <div
      className={cn(
        "plain-text-fallback whitespace-pre-wrap break-all font-mono text-sm",
        className,
      )}
    >
      {shown}
      {truncated && (
        <span className="text-muted-foreground">
          {`\n… [${text.length - MAX_PLAINTEXT_DISPLAY_LENGTH} more characters not shown]`}
        </span>
      )}
    </div>
  );
}

interface MarkdownNode {
  type: string;
  children?: MarkdownNode[];
}

/** Turns raw HTML nodes into text nodes holding the same source. */
function remarkHtmlAsText() {
  const visit = (node: MarkdownNode) => {
    if (node.type === "html") node.type = "text";
    node.children?.forEach(visit);
  };
  return visit;
}

/**
 * Wraps `MessageResponse` with {@link InlineCodeFileReference} via Streamdown's
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
 *
 * When `literalHtml` is set, raw HTML shows as written instead of rendering,
 * so markup or comments in a tool's text can't hide part of it.
 */
export function FilePathAwareMessageResponse({
  children,
  breaks = false,
  literalHtml = false,
  isStreaming = false,
  ...props
}: React.ComponentProps<typeof MessageResponse> & { breaks?: boolean; literalHtml?: boolean }) {
  const components = FILE_PATH_AWARE_COMPONENTS;

  // Extend (don't replace) Streamdown's defaults so remark-gfm survives. With
  // neither option set we pass `undefined` so Streamdown uses its defaults unchanged.
  const remarkPlugins = useMemo(
    () =>
      breaks || literalHtml
        ? [
            ...Object.values(defaultRemarkPlugins),
            ...(literalHtml ? [remarkHtmlAsText] : []),
            ...(breaks ? [remarkBreaks] : []),
          ]
        : undefined,
    [breaks, literalHtml],
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
    return <PlainTextFallback text={children as string} className={props.className} />;
  }

  return (
    <MessageResponse
      {...props}
      components={components}
      remarkPlugins={remarkPlugins}
      markFileLinks
      // The shown text can trail the latest by one interval after the stream ends.
      isStreaming={isStreaming || throttledText !== normalizedText}
    >
      {isString ? throttledText : children}
    </MessageResponse>
  );
}
