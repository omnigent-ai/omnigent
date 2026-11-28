// The file viewer's Markdown elements: workspace links open in the file viewer,
// Mermaid fences draw diagrams, and images keep their authored size.

import {
  createContext,
  isValidElement,
  useContext,
  type ComponentPropsWithoutRef,
  type ReactNode,
} from "react";
import type { Components } from "react-markdown";
import { cn } from "@/lib/utils";
import { ExternalPreviewLink } from "./ExternalPreviewLink";
import { useFileViewer } from "./FileViewerContext";
import { MermaidPreview } from "./MermaidPreview";
import { isWorkspaceRelativeSrc, resolveWorkspacePath } from "./TipTapWorkspaceImage";

// Path of the previewed file, so a relative link resolves against its directory.
export const MarkdownPreviewPathContext = createContext<string | null>(null);

/**
 * Anchor for the Markdown preview. A link to another workspace file (`notes.md`,
 * `../docs/`) opens in the FileViewer: as a plain anchor the browser resolves it
 * against the app URL (`/c/<id>` → `/c/notes.md`) and loads a session that
 * doesn't exist. URLs and `mailto:` links open outside the app window (see
 * ExternalPreviewLink); in-page `#` links keep their href.
 */
function MarkdownPreviewLink({
  href,
  children,
  node: _node,
  ...props
}: ComponentPropsWithoutRef<"a"> & { node?: unknown }) {
  const openFile = useFileViewer();
  const filePath = useContext(MarkdownPreviewPathContext);
  if (href && !isWorkspaceRelativeSrc(href)) {
    return (
      <ExternalPreviewLink href={href} {...props}>
        {children}
      </ExternalPreviewLink>
    );
  }
  if (!href || href.startsWith("#")) {
    return (
      <a href={href} {...props}>
        {children}
      </a>
    );
  }
  const target = filePath === null ? "" : resolveWorkspacePath(filePath, href);
  if (!openFile || !target) {
    return (
      <span className={props.className} title={href}>
        {children}
      </span>
    );
  }
  // No href, so a modified or middle click can't open the bogus app URL either.
  return (
    <a
      {...props}
      role="button"
      tabIndex={0}
      title={props.title ?? href}
      className={cn("cursor-pointer", props.className)}
      onClick={() => openFile(target)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openFile(target);
        }
      }}
    >
      {children}
    </a>
  );
}

export const MARKDOWN_PREVIEW_COMPONENTS: Components = {
  a: MarkdownPreviewLink,
  pre({ children, ...props }) {
    const child = isValidElement(children) ? children : null;
    if (
      isValidElement<{ className?: string; children?: ReactNode }>(child) &&
      // Match case-insensitively so a cased fence (```Mermaid) renders a diagram
      // in the read-only preview too, matching the editor's detection.
      child.props.className?.split(/\s+/).some((c) => c.toLowerCase() === "language-mermaid")
    ) {
      return <MermaidPreview source={String(child.props.children ?? "")} />;
    }
    return <pre {...props}>{children}</pre>;
  },
  // Tailwind Preflight applies `img { height: auto }`, which overrides the HTML
  // `width`/`height` *attributes* (presentational hints lose to any author CSS).
  // GitHub honors explicit dimensions, so mirror them onto an inline style —
  // which does win the cascade — for the one image being rendered. This runs
  // after sanitize (React components render the already-sanitized tree), so it
  // adds no attack surface. Only literal integer pixel values are forwarded.
  img({ node: _node, width, height, style, ...props }) {
    const px = (v: string | number | undefined) =>
      typeof v === "number" || (typeof v === "string" && /^\d+$/.test(v)) ? `${v}px` : undefined;
    const sized = {
      ...style,
      width: px(width) ?? style?.width,
      height: px(height) ?? style?.height,
    };
    return <img {...props} style={sized} />;
  },
};
