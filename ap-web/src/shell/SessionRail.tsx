// Right-side rail shown while viewing a conversation (`/c/:id`). A
// vertical stack of "session context" cards — terminals for now,
// sub-processes / progress / context to follow.
//
// The terminals card lists each open terminal as a clickable row.
// Clicking a row opens the right-side terminals push panel with
// that specific terminal active. There is no panel-only-open
// affordance: every WS attach is preceded by a deliberate row
// click, so the user always picks the terminal they want to see
// before the bridge connects.
//
// The execution-logs card lists the main thread plus each sub-agent
// (child) session. Clicking a row opens the execution-logs push
// panel scoped to that session, which renders the raw JSON items —
// parity with the TUI Ctrl+O overlay.

import {
  BotIcon,
  ChevronDownIcon,
  MessageSquareIcon,
  TerminalIcon,
  type LucideIcon,
} from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  executionLogTabKey,
  MAIN_EXECUTION_LOG_KEY,
  useChildSessions,
  type ChildSessionInfo,
} from "@/hooks/useChildSessions";
import { useDebugMode } from "@/hooks/useDebugMode";
import { terminalTabKey, useTerminals, type TerminalInfo } from "@/hooks/useTerminals";

interface SessionRailProps {
  conversationId: string;
  /**
   * Called when the user picks a terminal to view. Receives the
   * tab key for that terminal (e.g. ``"terminal:terminal_bash_s1"``).
   */
  onExpandTerminals: (initialKey: string) => void;
  /**
   * Called when the user picks an execution-log entry to view.
   * Receives the tab key — either ``"executionLog:main"`` or
   * ``"executionLog:<childSessionId>"``.
   */
  onExpandExecutionLogs: (initialKey: string) => void;
  /**
   * Hide the rail because a push panel is open and occupies the
   * same region. We fade rather than unmount so the rail's exit
   * and the panel's entry transition together.
   */
  suppressed: boolean;
}

export function SessionRail({
  conversationId,
  onExpandTerminals,
  onExpandExecutionLogs,
  suppressed,
}: SessionRailProps) {
  const { terminals } = useTerminals(conversationId);
  const { children } = useChildSessions(conversationId);
  const debugMode = useDebugMode();
  // The rail only earns its space when at least one of its cards
  // has content AND no push panel is already open. Terminals appear
  // when present; the execution-logs card is shown only in debug
  // mode (?debug=1), so the rail may be empty in normal usage.
  if (suppressed) return null;
  return (
    <>
      {terminals.length > 0 && <TerminalsCard terminals={terminals} onExpand={onExpandTerminals} />}
      {debugMode && <ExecutionLogsCard childSessions={children} onExpand={onExpandExecutionLogs} />}
    </>
  );
}

interface TerminalsCardProps {
  terminals: TerminalInfo[];
  onExpand: (initialKey: string) => void;
}

function TerminalsCard({ terminals, onExpand }: TerminalsCardProps) {
  const { t } = useTranslation("nav");
  const [collapsed, setCollapsed] = useState(false);
  return (
    <Card size="sm" data-testid="terminals-card">
      <CardHeader>
        <CardTitle className="text-sm">{t("terminals")}</CardTitle>
        <CardAction>
          <button
            type="button"
            aria-label={collapsed ? t("expandTerminals") : t("collapseTerminals")}
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
          {terminals.length === 0 ? (
            <p className="text-muted-foreground text-xs">{t("noOpenTerminals")}</p>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {terminals.map((t) => (
                <TerminalRow key={t.id} terminal={t} onOpen={() => onExpand(terminalTabKey(t))} />
              ))}
            </ul>
          )}
        </CardContent>
      )}
    </Card>
  );
}

function TerminalRow({ terminal, onOpen }: { terminal: TerminalInfo; onOpen: () => void }) {
  return (
    <li>
      <button
        type="button"
        data-testid="terminal-row"
        data-terminal-name={terminal.name}
        data-terminal-session={terminal.session}
        className="flex w-full cursor-pointer items-center gap-2 truncate rounded-md px-2 py-1 text-left text-xs hover:bg-muted"
        onClick={onOpen}
      >
        <TerminalIcon className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="truncate">{terminal.name}</span>
        <span className="shrink-0 text-muted-foreground">· {terminal.session}</span>
      </button>
    </li>
  );
}

interface ExecutionLogsCardProps {
  childSessions: ChildSessionInfo[];
  onExpand: (initialKey: string) => void;
}

function ExecutionLogsCard({ childSessions, onExpand }: ExecutionLogsCardProps) {
  const { t } = useTranslation("nav");
  const { t: tc } = useTranslation("common");
  const [collapsed, setCollapsed] = useState(false);
  return (
    <Card size="sm" data-testid="execution-logs-card">
      <CardHeader>
        <CardTitle className="text-sm">{tc("executionLogsTitle")}</CardTitle>
        <CardAction>
          <button
            type="button"
            aria-label={collapsed ? t("expandExecutionLogs") : t("collapseExecutionLogs")}
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
              label={t("mainThread")}
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
