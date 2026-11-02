// Reader mode for Markdown files. Loaded only when a Markdown file is opened.

import { Children, type ReactNode, isValidElement, useRef } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { resolveMarkdownLink } from "./markdownLinks.ts";

interface MarkdownReaderProps {
  path: string;
  content: string;
  /** Opens another file of the room's folder as a tab. */
  onOpenFile: (path: string) => void;
}

function textOf(children: ReactNode): string {
  return Children.toArray(children)
    .map((child) => (typeof child === "string" || typeof child === "number" ? String(child) : isValidElement<{ children?: ReactNode }>(child) ? textOf(child.props.children) : ""))
    .join("");
}

/** GitHub's heading anchors, so "#launch" links inside the document work. */
function slug(children: ReactNode): string {
  return textOf(children)
    .toLowerCase()
    .trim()
    .replace(/[^\p{L}\p{N}\s-]/gu, "")
    .replace(/\s/g, "-");
}

/** A Markdown file rendered for reading; raw HTML in the file is not rendered. */
export default function MarkdownReader({ path, content, onOpenFile }: MarkdownReaderProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const heading =
    (Tag: "h1" | "h2" | "h3" | "h4" | "h5" | "h6") =>
    ({ children }: { children?: ReactNode }) => <Tag id={`md-${slug(children)}`}>{children}</Tag>;

  const components: Components = {
    h1: heading("h1"),
    h2: heading("h2"),
    h3: heading("h3"),
    h4: heading("h4"),
    h5: heading("h5"),
    h6: heading("h6"),
    a: ({ href, children }) => {
      const link = resolveMarkdownLink(path, href ?? "");
      if (link.kind === "external") {
        return (
          <a href={link.href} target="_blank" rel="noopener noreferrer">
            {children}
          </a>
        );
      }
      if (link.kind === "blocked") return <span className="md-blocked-link">{children}</span>;
      return (
        <a
          href={href}
          title={link.kind === "file" ? `Open ${link.path}` : undefined}
          onClick={(event) => {
            event.preventDefault();
            if (link.kind === "file") onOpenFile(link.path);
            else containerRef.current?.querySelector(`#md-${CSS.escape(link.id)}`)?.scrollIntoView({ block: "start" });
          }}
        >
          {children}
        </a>
      );
    },
    // The office's content policy blocks outside images, and folder images are not served
    // here; show what the image is and let it be opened instead.
    img: ({ src, alt }) => {
      const link = typeof src === "string" ? resolveMarkdownLink(path, src) : ({ kind: "blocked" } as const);
      const label = `🖼 ${alt || "image"}`;
      if (link.kind === "file") {
        return (
          <button type="button" className="md-image" onClick={() => onOpenFile(link.path)} title={`Open ${link.path}`}>
            {label}
          </button>
        );
      }
      if (link.kind === "external") {
        return (
          <a className="md-image" href={link.href} target="_blank" rel="noopener noreferrer">
            {label}
          </a>
        );
      }
      return <span className="md-image">{label}</span>;
    },
  };

  return (
    <div className="markdown-reader" ref={containerRef}>
      <article className="markdown-body">
        <Markdown remarkPlugins={[remarkGfm]} components={components}>
          {content}
        </Markdown>
      </article>
    </div>
  );
}
