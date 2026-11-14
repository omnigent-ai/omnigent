import { Maximize2Icon, MessageSquareIcon, XIcon } from "lucide-react";

import { SideChatPane } from "@/components/chat/SideChatPane";
import { Button } from "@/components/ui/button";
import { WorkflowSessionForm } from "@/components/workflows/WorkflowSessionDialog";
import { useCanEdit } from "@/hooks/usePermissions";
import { useSession } from "@/hooks/useSession";
import { type WorkflowDetail, useSetWorkflowPlanningSession } from "@/hooks/useWorkflow";
import { Link } from "@/lib/routing";
import { readOnlyReasonForSessionLabels } from "@/lib/sessionCapabilities";
import { useChatStore } from "@/store/chatStore";

/** A real session attached to a workflow, with its own scoped composer and stream. */
export function WorkflowPlanningChat({
  workflow,
  onClose,
}: {
  workflow: WorkflowDetail;
  onClose: () => void;
}) {
  const sessionId = workflow.planning_session_id ?? null;
  const { session, error } = useSession(sessionId);
  const attach = useSetWorkflowPlanningSession(workflow.id);
  const needsSession = !sessionId || !!error || !!session?.archived;

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="workflow-planning-chat">
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-4 py-3">
        <MessageSquareIcon className="size-4 text-muted-foreground" aria-hidden="true" />
        <h2 className="min-w-0 flex-1 text-sm font-semibold">Create plan</h2>
        {sessionId && !needsSession && (
          <Button variant="ghost" size="icon-sm" asChild>
            <Link to={`/c/${sessionId}`} aria-label="Open planning session in full">
              <Maximize2Icon className="size-4" />
            </Link>
          </Button>
        )}
        <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label="Close planning chat">
          <XIcon className="size-4" />
        </Button>
      </div>
      {needsSession ? (
        <div className="flex min-h-0 flex-1 flex-col gap-4 p-4">
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
      ) : (
        <PlanningSession key={sessionId} sessionId={sessionId} />
      )}
    </div>
  );
}

function PlanningSession({ sessionId }: { sessionId: string }) {
  const { session } = useSession(sessionId);
  const canEdit = useCanEdit(sessionId);
  const readOnlyReason = readOnlyReasonForSessionLabels(session, null);
  return (
    <>
      <div className="min-h-0 flex-1">
        <SideChatPane
          childId={sessionId}
          fullSession
          readOnly={!canEdit || session === null || readOnlyReason !== null}
        />
      </div>
      {readOnlyReason && (
        <p className="px-4 py-2 text-sm text-muted-foreground">{readOnlyReason}</p>
      )}
    </>
  );
}
