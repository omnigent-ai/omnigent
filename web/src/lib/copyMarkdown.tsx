import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";
import Markdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

import { copyRichText, copyText } from "./clipboard";

// The slice of hast this module walks. @types/hast isn't a direct dependency,
// and the rehype pass below only needs elements, their href/alt, and children.
interface HastNode {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
}

// A scheme the paste target can resolve on its own. An in-app href
// ("src/App.tsx", "/c/123") would resolve against whatever app received the
// paste, landing there as a dead link.
const PASTEABLE_HREF = /^(?:https?|mailto):/i;

/**
 * Strips what only means something inside the app, so the fragment stands on
 * its own once pasted: images — which chat blocks in agent output anyway, so
 * carrying one out would load a remote URL the app itself refuses — and links
 * whose href isn't resolvable elsewhere, keeping their text.
 */
function rehypePasteable() {
  return (tree: HastNode) => {
    pruneChildren(tree);
  };
}

function pruneChildren(node: HastNode): void {
  if (!node.children) return;
  const kept: HastNode[] = [];
  for (const child of node.children) {
    pruneChildren(child);
    if (child.type !== "element") {
      kept.push(child);
      continue;
    }
    if (child.tagName === "img") {
      const alt = child.properties?.alt;
      if (typeof alt === "string" && alt) kept.push({ type: "text", value: alt });
      continue;
    }
    if (child.tagName === "a" && !PASTEABLE_HREF.test(String(child.properties?.href ?? ""))) {
      kept.push(...(child.children ?? []));
      continue;
    }
    kept.push(child);
  }
  node.children = kept;
}

/**
 * Renders markdown to an HTML fragment for the clipboard's `text/html` flavor.
 *
 * GFM plus inline HTML, matching how assistant bubbles render (remark-breaks is
 * a user-bubble concern): without the raw step a `<br>` between two words is
 * dropped and the words fuse. Sanitize then drops script, event handlers and
 * unsafe URLs, so agent output can't smuggle any of it into the app the user
 * pastes into.
 */
export function markdownToHtml(markdown: string): string {
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    flushSync(() => {
      root.render(
        <Markdown
          remarkPlugins={[remarkGfm]}
          rehypePlugins={[rehypeRaw, rehypeSanitize, rehypePasteable]}
        >
          {markdown}
        </Markdown>,
      );
    });
    // Raw-HTML reparsing leaves stray whitespace nodes around block
    // elements; they render as nothing but bloat the copied fragment.
    return container.innerHTML.trim();
  } finally {
    root.unmount();
  }
}

/**
 * Copies markdown as rendered HTML alongside its source, so pasting into Slack
 * or a doc keeps the formatting while a plain-text target still gets markdown.
 *
 * Rendering stays synchronous: an `await` before the clipboard write can outlive
 * the click's user-activation window and lose the write permission.
 */
export async function copyMarkdown(markdown: string): Promise<void> {
  let html: string;
  try {
    html = markdownToHtml(markdown);
  } catch {
    // A render failure must not cost the user their copy.
    await copyText(markdown);
    return;
  }
  await copyRichText({ html, text: markdown });
}
