// Comment-enabled HTML preview: renders agent-generated HTML in the same
// sandboxed iframe as the read-only preview, but injects a bridge script so
// users can select rendered text and attach review comments — parity with the
// Markdown (TipTap) and code (Monaco/Shiki) comment surfaces.
//
// The iframe stays sandboxed WITHOUT `allow-same-origin` (see HTML_PREVIEW_SANDBOX),
// so the parent can't touch its DOM directly. All selection capture and
// highlight painting happens inside the iframe via the injected bridge, relayed
// over a private MessageChannel. See htmlCommentBridge.ts for the protocol and
// trust model.

import { createPortal } from "react-dom";
import { useEffect, useMemo, useRef, useState } from "react";
import { MessageSquarePlusIcon } from "lucide-react";
import { type Comment } from "@/hooks/useComments";
import { useCanEdit } from "@/hooks/usePermissions";
import { getEmbedRoot } from "@/lib/host";
import { type ActiveSelection, HTML_PREVIEW_SANDBOX } from "./codeViewerHelpers";
import {
  anchorOccurrence,
  BRIDGE_MSG,
  BRIDGE_SOURCE,
  findAnchorInSource,
  injectCommentBridge,
  parseBridgeMessage,
} from "./htmlCommentBridge";
import { TruncatedBanner } from "./TruncatedBanner";

interface HtmlCommentViewerProps {
  conversationId: string;
  /** Raw HTML source — rendered in the iframe and searched for comment anchors. */
  content: string;
  truncated: boolean;
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (sel: ActiveSelection | null) => void;
}

/** Floating "Add comment" button position + the resolved selection it commits. */
interface FloatingAnchor {
  x: number;
  y: number;
  start_index: number;
  end_index: number;
  anchor_content: string;
}

function genNonce(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  // Deterministic-enough fallback for environments without crypto.randomUUID.
  return Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
}

/** Bridge payload for one comment: its id, anchor text, and which occurrence of
 * that text (by source offset) it belongs to — so repeated anchor text such as
 * a title reused in the body highlights only the commented instance. */
function commentPayload(content: string, c: Comment) {
  const anchor = c.anchor_content ?? "";
  return {
    id: c.id,
    anchor_content: anchor,
    occ: anchorOccurrence(content, anchor, c.start_index),
  };
}

/** Bridge payload for the active selection, or null. */
function activePayload(content: string, sel: ActiveSelection | null) {
  if (!sel) return null;
  return {
    anchor_content: sel.anchor_content,
    occ: anchorOccurrence(content, sel.anchor_content, sel.start_index),
  };
}

export function HtmlCommentViewer({
  conversationId,
  content,
  truncated,
  comments,
  activeSelection,
  onSetActiveSelection,
}: HtmlCommentViewerProps) {
  const canEdit = useCanEdit(conversationId);

  // A fresh nonce + srcDoc per content load. Changing srcDoc reloads the iframe
  // document, which re-runs the bridge and (via the new nonce) re-establishes
  // the channel — clearing any stale highlights from the previous content.
  const { nonce, srcDoc } = useMemo(() => {
    const n = genNonce();
    return { nonce: n, srcDoc: injectCommentBridge(content, n) };
  }, [content]);

  const iframeRef = useRef<HTMLIFrameElement>(null);
  const portRef = useRef<MessagePort | null>(null);
  const [floating, setFloating] = useState<FloatingAnchor | null>(null);

  // Latest values for the port message handler without re-establishing the channel.
  const commentsRef = useRef(comments);
  commentsRef.current = comments;
  const contentRef = useRef(content);
  contentRef.current = content;
  const onSetActiveSelectionRef = useRef(onSetActiveSelection);
  onSetActiveSelectionRef.current = onSetActiveSelection;
  const activeSelectionRef = useRef(activeSelection);
  activeSelectionRef.current = activeSelection;

  // Establish the MessageChannel once the iframe document has loaded. Parent-
  // initiated handshake (post init on load) avoids a ready/listen race.
  useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe) return;
    let channel: MessageChannel | null = null;

    const handleInbound = (raw: unknown) => {
      const msg = parseBridgeMessage(raw, nonce);
      if (!msg) return;
      if (msg.type === BRIDGE_MSG.ready) {
        // Flush current state now that the frame is connected.
        postState();
      } else if (msg.type === BRIDGE_MSG.selection) {
        const offsets = findAnchorInSource(contentRef.current, msg.text, msg.occ);
        // Selecting a range that already has a comment activates it (which
        // scrolls the panel to its card) rather than offering to add a new one.
        const existing =
          offsets &&
          commentsRef.current.find(
            (c) => c.start_index === offsets.start_index && c.end_index === offsets.end_index,
          );
        if (existing) {
          onSetActiveSelectionRef.current({
            start_index: existing.start_index,
            end_index: existing.end_index,
            anchor_content: existing.anchor_content ?? "",
          });
          setFloating(null);
          return;
        }
        const rect = iframe.getBoundingClientRect();
        setFloating({
          x: rect.left + msg.rect.left,
          y: rect.top + msg.rect.top - 6,
          start_index: offsets?.start_index ?? 0,
          end_index: offsets?.end_index ?? 0,
          anchor_content: msg.text,
        });
      } else if (msg.type === BRIDGE_MSG.commentClick) {
        const c = commentsRef.current.find((x) => x.id === msg.id);
        if (c) {
          onSetActiveSelectionRef.current({
            start_index: c.start_index,
            end_index: c.end_index,
            anchor_content: c.anchor_content ?? "",
          });
        }
        setFloating(null);
      } else if (msg.type === BRIDGE_MSG.selectionCleared) {
        onSetActiveSelectionRef.current(null);
        setFloating(null);
      }
    };

    const postState = () => {
      const port = portRef.current;
      if (!port) return;
      port.postMessage({
        source: BRIDGE_SOURCE,
        nonce,
        type: BRIDGE_MSG.setComments,
        comments: commentsRef.current.map((c) => commentPayload(contentRef.current, c)),
      });
      port.postMessage({
        source: BRIDGE_SOURCE,
        nonce,
        type: BRIDGE_MSG.setActive,
        active: activePayload(contentRef.current, activeSelectionRef.current),
      });
    };

    const onLoad = () => {
      const win = iframe.contentWindow;
      if (!win) return;
      channel?.port1.close();
      channel = new MessageChannel();
      channel.port1.onmessage = (ev) => handleInbound(ev.data);
      portRef.current = channel.port1;
      // targetOrigin "*" is required: the sandboxed frame has an opaque ("null")
      // origin, so we cannot name a concrete origin. The transferred port + the
      // nonce are the trust mechanism, not the origin.
      win.postMessage({ source: BRIDGE_SOURCE, nonce, type: BRIDGE_MSG.init }, "*", [
        channel.port2,
      ]);
    };

    iframe.addEventListener("load", onLoad);
    return () => {
      iframe.removeEventListener("load", onLoad);
      channel?.port1.close();
      portRef.current = null;
    };
  }, [nonce]);

  // Push comment-list changes into the frame.
  useEffect(() => {
    portRef.current?.postMessage({
      source: BRIDGE_SOURCE,
      nonce,
      type: BRIDGE_MSG.setComments,
      comments: comments.map((c) => commentPayload(content, c)),
    });
  }, [comments, content, nonce]);

  // Push active-selection changes into the frame (drives the active highlight).
  useEffect(() => {
    portRef.current?.postMessage({
      source: BRIDGE_SOURCE,
      nonce,
      type: BRIDGE_MSG.setActive,
      active: activePayload(content, activeSelection),
    });
  }, [activeSelection, content, nonce]);

  // Dismiss the floating button on any mousedown in the parent outside of it.
  // (Clicks inside the iframe are relayed as selection/clear messages instead.)
  useEffect(() => {
    const onMouseDown = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest("[data-add-comment-btn]")) setFloating(null);
    };
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, []);

  const preview = (
    <iframe
      ref={iframeRef}
      srcDoc={srcDoc}
      // oxlint-disable-next-line eslint-plugin-react(iframe-missing-sandbox)
      sandbox={HTML_PREVIEW_SANDBOX}
      title="HTML preview"
      className="w-full h-full border-0"
    />
  );

  return (
    <div className="flex h-full flex-col">
      {truncated && <TruncatedBanner />}
      <div className="min-h-0 flex-1">{preview}</div>
      {floating &&
        canEdit &&
        createPortal(
          <button
            data-add-comment-btn
            type="button"
            className="fixed z-50 flex items-center gap-1.5 rounded-md border border-border bg-popover backdrop-blur-xl backdrop-saturate-150 px-2.5 py-1 text-xs font-medium text-foreground shadow-md hover:bg-secondary transition-colors"
            style={{ left: floating.x, top: floating.y, transform: "translateY(-100%)" }}
            onClick={() => {
              onSetActiveSelection({
                start_index: floating.start_index,
                end_index: floating.end_index,
                anchor_content: floating.anchor_content,
              });
              setFloating(null);
            }}
          >
            <MessageSquarePlusIcon className="size-3.5" />
            Add comment
          </button>,
          getEmbedRoot() ?? document.body,
        )}
    </div>
  );
}
