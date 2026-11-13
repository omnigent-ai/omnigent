// The chat pane's own row with floating panes, at the top of a terminal-first session's card:
// it names the pane and holds the Chat/Terminal switch at its end, moved from the header.

import { useChatPaneViewToggleHostRef } from "./chatPaneToolbarHost";

export function ChatPaneStrip({ conversationId }: { conversationId: string }) {
  const viewToggleHostRef = useChatPaneViewToggleHostRef(conversationId);
  return (
    <div
      role="group"
      aria-label="Chat pane"
      data-chat-pane-strip=""
      className="relative z-10 flex h-7 shrink-0 items-center gap-1 px-1 text-xs"
    >
      {/* With one pane there's nothing to tell it from, so only assistive tech hears its name. */}
      <span className="sr-only">Chat</span>
      <span className="flex-1" />
      <span
        ref={viewToggleHostRef}
        data-chat-pane-view-toggle-host=""
        className="flex shrink-0 items-center empty:hidden"
      />
    </div>
  );
}
