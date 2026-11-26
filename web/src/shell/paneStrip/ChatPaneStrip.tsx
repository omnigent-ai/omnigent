// The chat pane's strip while the chat shows alone: the same row a pane in a split has, holding
// only the Chat/Terminal switch at its end; with no other pane there's nothing to name.

import { MessageSquareIcon } from "lucide-react";

import { useChatPaneViewToggleHostRef } from "../chatPaneToolbarHost";
import { PaneTitleStrip, type PaneStripPane } from "./PaneTitleStrip";
import type { PaneStripLayout } from "./paneStripLayout";

// Compact hides the icon and keeps the name for screen readers.
const ALONE_LAYOUT: PaneStripLayout = { compact: true, actions: 0, title: false };

export function ChatPaneStrip({ conversationId }: { conversationId: string }) {
  const viewToggleHostRef = useChatPaneViewToggleHostRef(conversationId);
  const pane: PaneStripPane = {
    name: "Chat",
    icon: <MessageSquareIcon />,
    commands: null,
    primary: true,
    viewToggleHostRef,
  };
  return (
    <PaneTitleStrip
      paneKey={`chat:${conversationId}`}
      pane={pane}
      layout={ALONE_LAYOUT}
      actionsShown={false}
      zoomed={false}
      onFocusChange={() => {}}
    />
  );
}
