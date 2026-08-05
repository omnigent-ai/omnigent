// Debug-mode execution-logs rail shown while viewing a conversation
// (`/c/:id`) when ``?debug=1`` is in the URL.
//
// The execution-logs card lists the main thread plus each sub-agent
// (child) session. Clicking a row opens the execution-logs push
// panel scoped to that session, which renders the raw JSON items —
// parity with the TUI Ctrl+O overlay.

import { BotIcon, ChevronDownIcon, MessageSquareIcon, type LucideIcon } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  executionLogTabKey,
  MAIN_EXECUTION_LOG_KEY,
  useChildSessions,
  type ChildSessionInfo,
} from "@/hooks/useChildSessions";

interface SessionRailProps {
  conversationId: string;
  /**
   * Called when the user picks an execution-log entry to view.
   * Receives the tab key — either ``"executionLog:main"`` or
   * ``"executionLog:<childSessionId>"``.
   */
  onExpandExecutionLogs: (initialKey: string) => void;
  /**
   * Hide the rail because a push panel is open and occupies the
   * same region. Returns null immediately when true.
   */
  suppressed: boolean;
}

export function SessionRail({
  conversationId,
  onExpandExecutionLogs,
  suppressed,
}: SessionRailProps) {
  const { children } = useChildSessions(conversationId);
  if (suppressed) return null;
  return <ExecutionLogsCard childSessions={children} onExpand={onExpandExecutionLogs} />;
}

interface ExecutionLogsCardProps {
  childSessions: ChildSessionInfo[];
  onExpand: (initialKey: string) => void;
}

function ExecutionLogsCard({ childSessions, onExpand }: ExecutionLogsCardProps) {
  const [collapsed, setCollapsed] = useState(false);
  return (
    <Card size="sm" data-testid="execution-logs-card">
      <CardHeader>
        <CardTitle className="text-ui truncate min-w-0">Execution logs</CardTitle>
        <CardAction>
          <button
            type="button"
            aria-label={collapsed ? "Expand execution logs" : "Collapse execution logs"}
            aria-expanded={!collapsed}
            className="cursor-pointer rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
            onClick={() => setCollapsed((v) => !v)}
          >
            <ChevronDownIcon
              className={cn(
                "size-3.5 transition-transform duration-150",
                collapsed && "-rotate-90",
              )}
            />
          </button>
        </CardAction>
      </CardHeader>
      {!collapsed && (
        <CardContent>
          <ul className="flex flex-col gap-0.5">
            <ExecutionLogRow
              label="main"
              sublabel={null}
              icon={MessageSquareIcon}
              onOpen={() => onExpand(executionLogTabKey(MAIN_EXECUTION_LOG_KEY))}
              testId="execution-log-row-main"
            />
            {childSessions.map((c) => (
              <ExecutionLogRow
                key={c.id}
                label={c.tool ?? c.title ?? c.id}
                sublabel={c.session_name}
                icon={BotIcon}
                onOpen={() => onExpand(executionLogTabKey(c.id))}
                testId="execution-log-row-child"
              />
            ))}
          </ul>
        </CardContent>
      )}
    </Card>
  );
}

function ExecutionLogRow({
  label,
  sublabel,
  icon: Icon,
  onOpen,
  testId,
}: {
  label: string;
  sublabel: string | null;
  /**
   * Lucide icon component rendered at the row's leading edge. Each
   * call site picks a glyph that matches the row's role —
   * ``MessageSquareIcon`` for the main thread, ``BotIcon`` for
   * sub-agent children — so the rail's roles are scannable at a
   * glance.
   */
  icon: LucideIcon;
  onOpen: () => void;
  testId: string;
}) {
  return (
    <li>
      <button
        type="button"
        data-testid={testId}
        className="flex w-full cursor-pointer items-center gap-2 truncate rounded-md px-2 py-1 text-left text-xs hover:bg-muted"
        onClick={onOpen}
      >
        <Icon className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="truncate">{label}</span>
        {sublabel && <span className="shrink-0 truncate text-muted-foreground">· {sublabel}</span>}
      </button>
    </li>
  );
}
