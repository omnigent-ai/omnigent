import type { ComponentPropsWithoutRef } from "react";
import { followLinkWithPopupFallback } from "@/lib/openLinkInApp";
import { useFileViewerConversationId } from "./FileViewerContext";

/**
 * Anchor for a URL (`https:`, `mailto:`, `//host`) in a read-only file preview
 * or the Markdown editor's link popup.
 * Shares chat's external-link handling, so a click opens outside the app window
 * instead of navigating the app itself off its server.
 */
export function ExternalPreviewLink({
  href,
  children,
  onClick,
  node: _node,
  ...props
}: ComponentPropsWithoutRef<"a"> & { href: string; node?: unknown }) {
  const conversationId = useFileViewerConversationId();
  return (
    <a
      {...props}
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(event) => {
        onClick?.(event);
        followLinkWithPopupFallback(event, href, conversationId);
      }}
    >
      {children}
    </a>
  );
}
