import type { ComponentProps, ReactNode } from "react";
import { cloneElement, isValidElement } from "react";

import { MarkdownErrorBoundary } from "./MarkdownErrorBoundary";

const LANGUAGE_CLASS = /language-([^\s]+)/;

/** Recover the raw source text of a fenced code block from its rendered children. */
export function extractCodeText(children: ReactNode): string {
  if (typeof children === "string" || typeof children === "number") {
    return String(children);
  }

  if (Array.isArray(children)) {
    return children.map(extractCodeText).join("");
  }

  if (isValidElement(children)) {
    const props = children.props as { children?: ReactNode; code?: unknown };
    if (typeof props.code === "string") {
      return props.code;
    }
    return extractCodeText(props.children);
  }

  return "";
}

function extractLanguage(children: ReactNode): string | undefined {
  if (!isValidElement(children)) return undefined;
  const { className } = children.props as { className?: string };
  return className?.match(LANGUAGE_CLASS)?.[1];
}

/**
 * Mirror Streamdown's default `pre`: tag the code child as a block element so
 * the code renderer takes the block path instead of the inline one.
 */
export function tagCodeBlock(children: ReactNode): ReactNode {
  return isValidElement(children)
    ? cloneElement(children, { "data-block": "true" } as Record<string, unknown>)
    : children;
}

/**
 * Plain, unhighlighted code block. Mirrors the markup, classes, and
 * `data-streamdown` attributes of Streamdown's own pre-highlight fallback so
 * existing CSS hooks (e.g. `.chat-code-wrap [data-streamdown="code-block-body"]`)
 * keep applying.
 */
function PlainCodeBlock({ code, language }: { code: string; language?: string }) {
  return (
    <div
      className="my-4 flex w-full flex-col gap-2 rounded-xl border border-border bg-sidebar p-2"
      data-language={language}
      data-streamdown="code-block"
    >
      <div
        className="flex h-8 items-center text-muted-foreground text-xs"
        data-language={language}
        data-streamdown="code-block-header"
      >
        <span className="ml-1 font-mono lowercase">{language}</span>
      </div>
      <div
        className="overflow-x-auto rounded-md border border-border bg-background p-4 text-sm"
        data-language={language}
        data-streamdown="code-block-body"
      >
        <pre>
          <code>{code}</code>
        </pre>
      </div>
    </div>
  );
}

/**
 * Contain a render fault to the one code block that threw.
 *
 * Streamdown loads its highlighted code body and mermaid renderer through
 * `React.lazy` chunks, and a chunk fetch that fails once (network blip, deploy
 * swapping hashed assets) is re-thrown on every later render. Without this
 * boundary the throw reaches the whole-message `MarkdownErrorBoundary`, which
 * replaces the entire message with "Could not render this markdown." until the
 * page is reloaded. Degrading just the block to its plain source keeps the
 * rest of the message rendered and readable.
 */
export function CodeBlockBoundary({ children }: { children: ReactNode }) {
  const code = extractCodeText(children);
  return (
    <MarkdownErrorBoundary
      source={code}
      fallback={<PlainCodeBlock code={code} language={extractLanguage(children)} />}
    >
      {children}
    </MarkdownErrorBoundary>
  );
}

/**
 * Drop-in `pre` override for Streamdown seams that don't otherwise customize
 * code blocks: replicates the default `pre` and adds per-block containment.
 */
export function CodeBlockGuardedPre({ children }: ComponentProps<"pre">) {
  return <CodeBlockBoundary>{tagCodeBlock(children)}</CodeBlockBoundary>;
}
