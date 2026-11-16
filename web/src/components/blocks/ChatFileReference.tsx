// Renderers that turn file and folder references in chat markdown into links
// opening the FileViewer (or the Files tree for a folder). Every surface parses
// with `lib/fileReferences` and resolves with `useFileReference`, so an inline
// code span, a markdown link, a bare prose path and a mention pill naming the
// same file all behave the same way.

import type React from "react";
import { useMemo } from "react";
import { CopyIcon } from "lucide-react";
import { toast } from "sonner";
import {
  INLINE_CODE_LINE_NOTE_ATTR,
  PROSE_FILE_REFERENCE_ENDS_ATTR,
} from "@/components/ai-elements/streamdown-security";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { showToast } from "@/components/ui/toast";
import { VideoPlayer } from "@/components/VideoPlayer";
import {
  resolveReferenceCandidate,
  type ResolvedFileReference,
  useFileReference,
} from "@/hooks/useFileReference";
import { copyText } from "@/lib/clipboard";
import {
  mergeProseExtents,
  parseFileReference,
  shortFileCitationLabel,
  splitFileCitation,
} from "@/lib/fileReferences";
import { cn } from "@/lib/utils";
import { isVideoFile } from "@/lib/video";
import { useFileViewerConversationId, useWorkspacePaths } from "@/shell/FileViewerContext";

// Streamdown hands each component override the source hast node alongside the
// element's own props; spread onto a DOM element it renders node="[object Object]".
export type WithHastNode<T> = T & { node?: unknown };

// Streamdown's own anchor styling. index.css keys the pointer cursor and the
// table-cell `overflow-wrap` rule on the `data-streamdown="link"` attribute.
export const STREAMDOWN_LINK_CLASS = "wrap-anywhere font-medium text-link underline";
// A dotted underline marks "opens in the app" apart from a link that leaves it.
const FILE_LINK_CLASS = cn(STREAMDOWN_LINK_CLASS, "decoration-dotted underline-offset-2");

/** Keyboard activation for a non-button element given `role="button"`. */
function activateOnKey(action: () => void) {
  return (event: React.KeyboardEvent) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      action();
    }
  };
}

function lineNotePosition(note: unknown): { line: number; endLine?: number } | null {
  if (typeof note !== "string") return null;
  const [line, endLine] = note.split("-").map(Number);
  if (!Number.isSafeInteger(line) || line <= 0) return null;
  return { line, ...(Number.isSafeInteger(endLine) && endLine > line ? { endLine } : {}) };
}

/**
 * Inline `code` that names an existing file or folder renders as a link; any
 * other span (`git status`, `useState`, a missing path) stays styled code.
 * A link shows the file name and position; the full text stays its accessible
 * name and hover title.
 */
export function InlineCodeFileReference({
  children,
  className,
  node: _node,
  [INLINE_CODE_LINE_NOTE_ATTR]: lineNote,
  ...codeProps
}: WithHastNode<React.ComponentPropsWithoutRef<"code">> & {
  [INLINE_CODE_LINE_NOTE_ATTR]?: string;
}) {
  const text = typeof children === "string" ? children : "";
  const reference = useMemo(() => parseFileReference(text, "code"), [text]);
  const position = useMemo(
    () => (reference?.citation.hasPosition ? null : lineNotePosition(lineNote)),
    [reference, lineNote],
  );
  const { open } = useFileReference(reference, { position });

  if (open) {
    const shortLabel = shortFileCitationLabel(text);
    // An inline <code>, not a <button>: a button is an atomic inline-block,
    // so a long path couldn't wrap across lines.
    return (
      <code
        role="button"
        tabIndex={0}
        data-streamdown="inline-code"
        className={cn(
          "font-mono text-ui text-link underline decoration-dotted underline-offset-2 transition-colors cursor-pointer",
          className,
        )}
        aria-label={shortLabel ? text : undefined}
        title={shortLabel ? text : undefined}
        onClick={open}
        onKeyDown={activateOnKey(open)}
        {...codeProps}
      >
        {shortLabel ?? children}
      </code>
    );
  }
  // Streamdown's default inline-code styling, so non-path code looks unchanged.
  return (
    <code
      className={cn("rounded bg-muted px-1.5 py-0.5 font-mono text-ui", className)}
      data-streamdown="inline-code"
      {...codeProps}
    >
      {children}
    </code>
  );
}

/**
 * A path found in plain prose. It links once a check finds the file; until
 * then, and when nothing is there, it is the text as written. A path that may
 * run on through spaces links the longest stretch that names a real file.
 */
export function ProseFileReference({
  children,
  node: _node,
  [PROSE_FILE_REFERENCE_ENDS_ATTR]: ends,
}: WithHastNode<{ children?: React.ReactNode }> & { [PROSE_FILE_REFERENCE_ENDS_ATTR]?: string }) {
  const text = typeof children === "string" ? children : "";
  const merged = useMemo(() => {
    const lengths = (ends ?? String(text.length)).split(",").map(Number);
    return mergeProseExtents(text, lengths);
  }, [text, ends]);
  const reference = merged?.reference ?? null;
  const resolved = useFileReference(reference, {
    // Searching for a bare prose word is too noisy; a directory or position is evidence.
    search:
      !!reference &&
      merged?.candidateLengths.length === 1 &&
      (reference.hasDirectory || reference.citation.hasPosition),
  });
  if (!resolved.open || !merged) return children;
  const length =
    resolved.candidateIndex >= 0 ? merged.candidateLengths[resolved.candidateIndex] : text.length;
  const linked = text.slice(0, length);
  return (
    <>
      <FileLinkContextMenu path={resolved.copyPath}>
        <a
          role="button"
          tabIndex={0}
          data-streamdown="link"
          className={FILE_LINK_CLASS}
          title={resolved.path}
          onClick={resolved.open}
          onKeyDown={activateOnKey(resolved.open)}
        >
          {linked}
        </a>
      </FileLinkContextMenu>
      {text.slice(length)}
    </>
  );
}

/**
 * A markdown link whose target is a local path (moved onto a data attribute by
 * `markWorkspaceFileLinks`, its href parked on an inert fragment). It opens the
 * FileViewer or the Files tree, plays a video, or, when the path names nothing,
 * stays text: with feedback on activation once the path is known to be dead.
 * Every form offers "Copy path" on right-click, since there is no href to copy.
 */
export function MarkdownFileLink({
  path,
  children,
  className,
  title,
  ...props
}: Omit<React.ComponentPropsWithoutRef<"a">, "href"> & { path: string }) {
  const reference = useMemo(() => parseFileReference(path, "link"), [path]);
  const resolved = useFileReference(reference);
  const conversationId = useFileViewerConversationId();
  const { root, home } = useWorkspacePaths();

  if (!resolved.open) {
    if (resolved.status === "missing" || resolved.status === "invalid") {
      const explain = () =>
        showToast(
          resolved.status === "missing" && resolved.path
            ? `Can't open ${resolved.path}: it wasn't found in this session's reachable filesystem.`
            : `Can't open ${path}: it doesn't resolve to an openable file path.`,
        );
      return (
        <FileLinkContextMenu path={resolved.copyPath}>
          <span
            role="button"
            tabIndex={0}
            className={cn(
              "cursor-pointer text-muted-foreground underline decoration-dotted underline-offset-2",
              className,
            )}
            title={title ?? path}
            onClick={explain}
            onKeyDown={activateOnKey(explain)}
          >
            {children}
          </span>
        </FileLinkContextMenu>
      );
    }
    // Not verified yet, or a folder with no Files tree to show it.
    return (
      <FileLinkContextMenu path={resolved.copyPath}>
        <span className={className} title={title ?? path}>
          {children}
        </span>
      </FileLinkContextMenu>
    );
  }

  if (conversationId && resolved.status === "file" && isVideoFile(resolved.path)) {
    return (
      <VideoPlayer
        compact
        conversationId={conversationId}
        path={resolved.path}
        title={
          typeof children === "string"
            ? children
            : (resolved.path.split("/").pop() ?? resolved.path)
        }
        onOpenFile={resolved.open}
      />
    );
  }

  // Link text that spells out the linked file and position shows just its name;
  // a descriptive label, or text citing a different place, is kept as written.
  const linkText = typeof children === "string" ? children : null;
  const shortLabel =
    linkText && citesSamePlace(linkText, path, resolved, root, home)
      ? shortFileCitationLabel(linkText)
      : null;

  return (
    <FileLinkContextMenu path={resolved.copyPath}>
      <a
        {...props}
        role="button"
        tabIndex={0}
        aria-label={shortLabel && linkText ? linkText : props["aria-label"]}
        title={title ?? path}
        data-streamdown="link"
        className={cn(FILE_LINK_CLASS, className)}
        onClick={resolved.open}
        onKeyDown={activateOnKey(resolved.open)}
      >
        {shortLabel ?? children}
      </a>
    </FileLinkContextMenu>
  );
}

/** Whether link text names the same file and position the link opens. */
function citesSamePlace(
  text: string,
  target: string,
  resolved: ResolvedFileReference,
  root: string | null,
  home: string | null,
): boolean {
  const cited = splitFileCitation(text);
  const linked = splitFileCitation(target);
  const citedPath = resolveReferenceCandidate(cited.path.replace(/^(\.\/)+/, ""), root, home);
  return (
    !!citedPath &&
    citedPath !== "pending" &&
    citedPath.path === resolved.path &&
    (!cited.hasPosition ||
      (cited.line === linked.line &&
        cited.column === linked.column &&
        cited.endLine === linked.endLine))
  );
}

/** Opens a tagged file or folder from a sent message's mention pill. */
export function useMentionedFile(path: string): (() => void) | null {
  const reference = useMemo(() => parseFileReference(path, "link"), [path]);
  return useFileReference(reference).open;
}

/** Right-click menu that copies the path a file link names. */
function FileLinkContextMenu({ path, children }: { path: string; children: React.ReactElement }) {
  const copyPath = () =>
    void copyText(path).then(
      () => toast.success("Copied path"),
      () => toast.error("Couldn't copy the path"),
    );
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
      <ContextMenuContent>
        <ContextMenuItem onSelect={copyPath}>
          <CopyIcon />
          Copy path
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  );
}
