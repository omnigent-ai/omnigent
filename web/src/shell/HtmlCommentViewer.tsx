// Comment-enabled HTML preview: renders agent-generated HTML in the same
// sandboxed iframe as the read-only preview, but injects a bridge script so
// users can select rendered text and attach review comments — parity with the
// Markdown (TipTap) and code (Monaco/Shiki) comment surfaces.
//
// The iframe stays sandboxed WITHOUT `allow-same-origin` (see HTML_PREVIEW_SANDBOX),
// so the parent can't touch its DOM directly. All selection capture and
// highlight painting happens inside the iframe via the injected bridge, relayed
// over a private MessageChannel. See htmlCommentBridge.ts for the protocol and
// trust model; parent-side handshake/UI lives in useHtmlCommentBridge.

import { useMemo } from "react";
import type { Comment } from "@/hooks/useComments";
import { type ActiveSelection, HTML_PREVIEW_SANDBOX } from "./codeViewerHelpers";
import { injectCommentBridge } from "./htmlCommentBridge";
import { TruncatedBanner } from "./TruncatedBanner";
import { useHtmlCommentBridge } from "./useHtmlCommentBridge";

interface HtmlCommentViewerProps {
  conversationId: string;
  /** Raw HTML source — rendered in the iframe and searched for comment anchors. */
  content: string;
  truncated: boolean;
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (sel: ActiveSelection | null) => void;
}

export function HtmlCommentViewer({
  conversationId,
  content,
  truncated,
  comments,
  activeSelection,
  onSetActiveSelection,
}: HtmlCommentViewerProps) {
  const { nonce, iframeRef, addCommentPortal } = useHtmlCommentBridge({
    conversationId,
    content,
    docKey: content,
    comments,
    activeSelection,
    onSetActiveSelection,
  });

  const srcDoc = useMemo(() => injectCommentBridge(content, nonce), [content, nonce]);

  return (
    <div className="flex h-full flex-col">
      {truncated && <TruncatedBanner />}
      <div className="min-h-0 flex-1">
        <iframe
          ref={iframeRef}
          srcDoc={srcDoc}
          sandbox={HTML_PREVIEW_SANDBOX}
          title="HTML preview"
          className="w-full h-full border-0"
        />
      </div>
      {addCommentPortal}
    </div>
  );
}
