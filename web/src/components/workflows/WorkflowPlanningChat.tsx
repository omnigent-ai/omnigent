import {
  ArrowUpRightIcon,
  Maximize2Icon,
  MessageSquareIcon,
  Minimize2Icon,
  XIcon,
} from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

import { FullSessionChat } from "@/components/chat/FullSessionChat";
import { Button } from "@/components/ui/button";
import { WorkflowSessionForm } from "@/components/workflows/WorkflowSessionDialog";
import { useSession } from "@/hooks/useSession";
import { type WorkflowDetail, useSetWorkflowPlanningSession } from "@/hooks/useWorkflow";
import { Link } from "@/lib/routing";
import { ApiError } from "@/lib/sessionsApi";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";

/** A real session attached to a workflow, with its own scoped composer and stream. */
export function WorkflowPlanningChat({
  workflow,
  onClose,
  expanded = false,
  onExpandedChange,
}: {
  workflow: WorkflowDetail;
  onClose: () => void;
  /** Whether the page shows the panel enlarged in place; the toggle needs `onExpandedChange`. */
  expanded?: boolean;
  onExpandedChange?: (expanded: boolean) => void;
}) {
  const sessionId = workflow.planning_session_id ?? null;
  const queryClient = useQueryClient();
  const { session, error } = useSession(sessionId);
  const attach = useSetWorkflowPlanningSession(workflow.id);
  const needsSession =
    !sessionId || (error instanceof ApiError && error.status === 404) || !!session?.archived;

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="workflow-planning-chat">
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-4 py-3">
        <MessageSquareIcon className="size-4 text-muted-foreground" aria-hidden="true" />
        <h2 className="min-w-0 flex-1 text-sm font-semibold">
          {workflow.steps.length > 0 ? "Edit plan" : "Create plan"}
        </h2>
        {sessionId && !needsSession && (
          <Button variant="ghost" size="icon-sm" asChild>
            <Link to={`/c/${sessionId}`} aria-label="Open planning session" title="Open session">
              <ArrowUpRightIcon className="size-4" />
            </Link>
          </Button>
        )}
        {onExpandedChange && (
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => onExpandedChange(!expanded)}
            aria-label={expanded ? "Collapse planning chat" : "Expand planning chat"}
            title={expanded ? "Collapse" : "Expand"}
          >
            {expanded ? <Minimize2Icon className="size-4" /> : <Maximize2Icon className="size-4" />}
          </Button>
        )}
        <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label="Close planning chat">
          <XIcon className="size-4" />
        </Button>
      </div>
      {needsSession ? (
        <div
          className={cn(
            "flex min-h-0 flex-1 flex-col gap-4 p-4",
            // The setup form keeps the side panel's width when the panel fills the page.
            expanded && "mx-auto w-full max-w-xl",
          )}
        >
          <WorkflowSessionForm
            request={{ kind: "plan", workflow }}
            onClose={onClose}
            onStarted={async (created, initialPrompt) => {
              await attach.mutateAsync(created.id);
              if (initialPrompt) {
                useChatStore.setState((state) => ({
                  sideChatDrafts: { ...state.sideChatDrafts, [created.id]: initialPrompt },
                }));
              }
              useChatStore.getState().requestSideChatFocus(created.id);
            }}
          />
        </div>
      ) : error && !session ? (
        <div className="flex flex-col items-start gap-2 p-4">
          <p role="alert" className="text-sm text-destructive">
            Couldn’t load the planning chat.
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void queryClient.invalidateQueries({ queryKey: ["session", sessionId] })}
          >
            Try again
          </Button>
        </div>
      ) : (
        <FullSessionChat key={sessionId} sessionId={sessionId} />
      )}
    </div>
  );
}
