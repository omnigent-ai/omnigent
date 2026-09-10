import { useEffect, useRef, useState, type ReactNode } from "react";
import { useParams } from "@/lib/routing";
import { BookOpen, MessageSquare } from "lucide-react";
import { authenticatedFetch } from "@/lib/identity";
import { mountNotebookPane } from "./notebook-pane.mjs";
import "./docloop-chat-layout.css";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";

/** Keeps native ChatPage mounted: its model picker, composer and streaming are unchanged. */
export function DocloopChatLayout({ children }: { children: ReactNode }) {
  const info = useServerInfo();
  const enabled = isFeatureEnabled(info, "docloop_notebook");
  const { conversationId } = useParams<{ conversationId: string }>();
  const [open, setOpen] = useState(false);
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!enabled || !open || !conversationId || !panel.current) return;
    const pane = mountNotebookPane(panel.current, {
      sessionId: conversationId,
      fetcher: authenticatedFetch,
    });
    return () => pane.dispose();
  }, [enabled, open, conversationId]);
  if (!enabled) return children;
  return (
    <div className="docloop-chat-container">
      <div className={`docloop-chat-layout${open && conversationId ? " docloop-open" : ""}`}>
        <div className="docloop-native-chat">{children}</div>
        <div className="docloop-pane-toggle" role="group" aria-label="Session view">
          <button type="button" aria-pressed={!open} onClick={() => setOpen(false)}>
            <MessageSquare size={16} aria-hidden="true" /> Chat
          </button>
          <button
            type="button"
            disabled={!conversationId}
            aria-pressed={open}
            aria-expanded={open && !!conversationId}
            aria-controls="docloop-notebook-pane"
            onClick={() => setOpen((value) => !value)}
          >
            <BookOpen size={16} aria-hidden="true" /> Notebook
          </button>
        </div>
        <aside
          id="docloop-notebook-pane"
          aria-label="Session notebook"
          hidden={!open || !conversationId}
          ref={panel}
        />
      </div>
    </div>
  );
}
