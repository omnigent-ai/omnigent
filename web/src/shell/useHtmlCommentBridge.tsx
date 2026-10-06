// Parent-side HTML comment bridge: MessageChannel handshake, floating
// Add-comment button, and source-offset anchoring. Shared by HtmlCommentViewer,
// SlidesViewer, and WireframeViewer so all three keep the same comment UX.

import { createPortal } from "react-dom";
import { useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { MessageSquarePlusIcon } from "lucide-react";
import type { Comment } from "@/hooks/useComments";
import { useCanEdit } from "@/hooks/usePermissions";
import { getEmbedRoot } from "@/lib/host";
import { randomUUID } from "@/lib/randomUUID";
import type { ActiveSelection } from "./codeViewerHelpers";
import {
  anchorOccurrence,
  BRIDGE_MSG,
  BRIDGE_SOURCE,
  findAnchorInSource,
  mapBridgeRectToViewport,
  parseBridgeMessage,
} from "./htmlCommentBridge";

/** Floating Add-comment button position + the resolved selection it commits. */
export interface FloatingAnchor {
  x: number;
  y: number;
  start_index: number;
  end_index: number;
  anchor_content: string;
}

function genNonce(): string {
  return randomUUID();
}

/** Bridge payload for one comment: id, anchor text, and occurrence index. */
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
    comment_id: sel.comment_id,
  };
}

export interface UseHtmlCommentBridgeArgs {
  conversationId: string;
  /** Workspace file content used for source-offset anchoring (not the injected preview doc). */
  content: string;
  /** When this changes, a fresh nonce is issued (reloads the bridge channel). */
  docKey: string;
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (sel: ActiveSelection | null) => void;
  /**
   * CSS scale applied to the iframe (slide/wireframe stages). Selection rects
   * from the frame are in unscaled coordinates; multiply by this for the host.
   */
  scale?: number;
}

export interface UseHtmlCommentBridgeResult {
  nonce: string;
  iframeRef: RefObject<HTMLIFrameElement | null>;
  /** Portalled floating Add-comment control (or null). */
  addCommentPortal: ReactNode;
}

/**
 * Wire a sandboxed preview iframe to the comment bridge: handshake on load,
 * push comment/active state, show the floating Add-comment button on selection.
 */
export function useHtmlCommentBridge({
  conversationId,
  content,
  docKey,
  comments,
  activeSelection,
  onSetActiveSelection,
  scale = 1,
}: UseHtmlCommentBridgeArgs): UseHtmlCommentBridgeResult {
  const canEdit = useCanEdit(conversationId);
  const nonce = useMemo(() => {
    void docKey;
    return genNonce();
  }, [docKey]);
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const portRef = useRef<MessagePort | null>(null);
  const [floating, setFloating] = useState<FloatingAnchor | null>(null);
  const scaleRef = useRef(scale);
  scaleRef.current = scale;

  const commentsRef = useRef(comments);
  commentsRef.current = comments;
  const contentRef = useRef(content);
  contentRef.current = content;
  const onSetActiveSelectionRef = useRef(onSetActiveSelection);
  onSetActiveSelectionRef.current = onSetActiveSelection;
  const activeSelectionRef = useRef(activeSelection);
  activeSelectionRef.current = activeSelection;

  useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe) return;
    let channel: MessageChannel | null = null;

    const handleInbound = (raw: unknown) => {
      const msg = parseBridgeMessage(raw, nonce);
      if (!msg) return;
      if (msg.type === BRIDGE_MSG.ready) {
        postState();
      } else if (msg.type === BRIDGE_MSG.selection) {
        const offsets = findAnchorInSource(contentRef.current, msg.text, msg.occ);
        const existing =
          offsets &&
          commentsRef.current.find(
            (c) =>
              c.status === "draft" &&
              c.start_index === offsets.start_index &&
              c.end_index === offsets.end_index,
          );
        if (existing) {
          onSetActiveSelectionRef.current({
            start_index: existing.start_index,
            end_index: existing.end_index,
            anchor_content: existing.anchor_content ?? "",
            comment_id: existing.id,
          });
          setFloating(null);
          return;
        }
        const rect = iframe.getBoundingClientRect();
        const pos = mapBridgeRectToViewport(rect, msg.rect, scaleRef.current);
        setFloating({
          x: pos.x,
          y: pos.y,
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
            comment_id: c.id,
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

  useEffect(() => {
    portRef.current?.postMessage({
      source: BRIDGE_SOURCE,
      nonce,
      type: BRIDGE_MSG.setComments,
      comments: comments.map((c) => commentPayload(content, c)),
    });
  }, [comments, content, nonce]);

  useEffect(() => {
    portRef.current?.postMessage({
      source: BRIDGE_SOURCE,
      nonce,
      type: BRIDGE_MSG.setActive,
      active: activePayload(content, activeSelection),
    });
  }, [activeSelection, content, nonce]);

  useEffect(() => {
    const onMouseDown = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest("[data-add-comment-btn]")) setFloating(null);
    };
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, []);

  const addCommentPortal =
    floating && canEdit
      ? createPortal(
          <button
            data-add-comment-btn
            type="button"
            className="fixed z-50 flex items-center gap-1.5 rounded-md border border-border bg-popover backdrop-blur-xl backdrop-saturate-150 px-2.5 py-1 text-sm font-medium text-foreground shadow-md hover:bg-secondary transition-colors"
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
        )
      : null;

  return { nonce, iframeRef, addCommentPortal };
}
