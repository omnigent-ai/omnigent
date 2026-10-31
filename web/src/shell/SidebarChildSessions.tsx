import { useChildSessions } from "@/hooks/useChildSessions";
import { Link, useLocation } from "@/lib/routing";
import { sessionNavigationSearch } from "@/lib/sessionNavigation";
import { childStatus } from "./subagentStatus";
import type { MouseEventHandler } from "react";

export function SidebarChildSessions({
  conversationId,
  onNavigate,
}: {
  conversationId: string;
  onNavigate?: MouseEventHandler<HTMLAnchorElement>;
}) {
  const { children } = useChildSessions(conversationId);
  const location = useLocation();
  const visible = children.filter((child) => child.labels?.["omnigent.closed"] !== "true");
  if (visible.length === 0) return null;

  return (
    <li className="ml-3 border-l border-border pl-2" data-testid="sidebar-child-sessions">
      <details open>
        <summary className="cursor-pointer py-1 text-xs text-muted-foreground">
          {visible.length} child {visible.length === 1 ? "chat" : "chats"}
        </summary>
        <ul aria-label="Child chats" className="flex flex-col gap-px">
          {visible.map((child) => {
            const title = child.task_summary || child.session_name || child.title || child.id;
            const status = childStatus(child).label;
            return (
              <li key={child.id}>
                <Link
                  onClick={onNavigate}
                  aria-label={`${title}: ${status}`}
                  to={{
                    pathname: `/c/${encodeURIComponent(child.id)}`,
                    search: sessionNavigationSearch(location.search),
                  }}
                  className="flex items-center gap-2 rounded px-2 py-1 text-xs hover:bg-accent focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                >
                  <span className="min-w-0 flex-1 truncate" title={title}>
                    {title}
                  </span>
                  <span className="shrink-0 text-muted-foreground">{status}</span>
                </Link>
              </li>
            );
          })}
        </ul>
      </details>
    </li>
  );
}
