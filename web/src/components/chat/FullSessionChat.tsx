import { useEffect, useMemo, type ComponentProps } from "react";
import { SideChatPane } from "@/components/chat/SideChatPane";
import type { ChatPresentation } from "./assistantPresentation";
import { HostBadge } from "@/components/HostBadge";
import {
  ScopedSessionHarnessPicker,
  SessionPermissionPicker,
} from "@/components/composer/ScopedSessionComposerControls";
import { ComposerWorkspaceBar } from "@/components/composer/ComposerControls";
import { ComposerWorkspaceStatus } from "@/components/composer/ComposerWorkspaceStatus";
import { ComposerContextRing } from "@/components/composer/ComposerContextRing";
import { useSharedSessionDraft } from "@/grid/sharedSessionDraft";
import { useGridPermissionControl } from "@/grid/useGridPermissionControl";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";
import { useComposerGitStatus } from "@/hooks/useComposerGitStatus";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useRunnerHealthRegistration, useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { livenessRowFromSession, useSessionLiveness } from "@/hooks/useSessionLiveness";
import { useCanEdit } from "@/hooks/usePermissions";
import { useSession } from "@/hooks/useSession";
import { readOnlyReasonForSessionLabels } from "@/lib/sessionCapabilities";
import { composerContextFromLabels } from "@/lib/composerContextAdapters";
import { hasPendingOwnElicitation } from "@/lib/blocks";
import type { Session } from "@/lib/types";
import { consumePendingInitialPrompt, useChatStore } from "@/store/chatStore";

/** An existing session's full history and its own composer, read-only when it can't take turns. */
export function FullSessionChat({
  sessionId,
  evenComposerPadding = false,
  transcriptTopInset,
  sessionComposer = false,
  presentation,
}: {
  sessionId: string;
  /** Space below the composer matches its sides, for a pane that frames it on all edges. */
  evenComposerPadding?: boolean;
  /** Extra room above the transcript, as a CSS length, for a header laid over it. */
  transcriptTopInset?: string;
  /** Reuse the full session's draft and session-scoped composer controls. */
  sessionComposer?: boolean;
  presentation?: ChatPresentation;
}) {
  const { session } = useSession(sessionId);
  const canEdit = useCanEdit(sessionId);
  const readOnlyReason = readOnlyReasonForSessionLabels(session, null);
  const pane = {
    childId: sessionId,
    fullSession: true,
    evenComposerPadding,
    transcriptTopInset,
    presentation,
    readOnly: !canEdit || session === null || readOnlyReason !== null,
  };
  return (
    <>
      <div className="min-h-0 flex-1">
        {sessionComposer ? (
          <SessionChatPane {...pane} session={session} />
        ) : (
          <SideChatPane {...pane} />
        )}
      </div>
      {readOnlyReason && (
        <p className="px-4 py-2 text-sm text-muted-foreground">{readOnlyReason}</p>
      )}
    </>
  );
}

function SessionChatPane({
  session,
  ...pane
}: ComponentProps<typeof SideChatPane> & { session: Session | null }) {
  const sessionId = pane.childId;
  useSharedSessionDraft(sessionId);
  const state = useConversationEntryState(sessionId);
  const healthSessions = useMemo(() => [{ id: sessionId }], [sessionId]);
  useRunnerHealthRegistration(healthSessions);
  const runnerOnline = useSessionRunnerOnline(sessionId);
  const liveness = useSessionLiveness(sessionId, livenessRowFromSession(session), {
    turnActive: state.status === "streaming",
    launchedAt: state.runnerLaunchedAt,
  });
  const unreachable = liveness.kind === "host_offline" || liveness.kind === "local_stranded";
  const permission = useGridPermissionControl(
    session,
    pane.readOnly ? 1 : (session?.permissionLevel ?? null),
    unreachable,
  );
  useEffect(() => {
    if (pane.readOnly || !session?.agentId || state.loadingConversation) return;
    const prompt = consumePendingInitialPrompt(sessionId);
    if (!prompt) return;
    const store = useChatStore.getState();
    const target = { pinnedConversationId: sessionId };
    if (prompt.skill) {
      void store.sendSlashCommand(prompt.skill.name, prompt.skill.args, session.agentId, target);
    } else {
      void store.send(prompt.text, session.agentId, prompt.files, target);
    }
  }, [pane.readOnly, session?.agentId, sessionId, state.loadingConversation]);
  return (
    <SideChatPane
      {...pane}
      gridComposer={{
        workspace: session && <SessionWorkspace session={session} />,
        leading: session && (
          <>
            <HostBadge
              sessionId={sessionId}
              appearance="composer"
              readOnly={pane.readOnly}
              launch={state.sandboxStatus}
            />
            <SessionPermissionPicker permission={permission} />
          </>
        ),
        trailing: session && (
          <ScopedSessionHarnessPicker
            sessionId={sessionId}
            session={session}
            canEdit={!pane.readOnly}
            unreachable={unreachable}
            runnerOnline={runnerOnline}
            agentIcon={pane.presentation?.assistant?.icon}
          />
        ),
        cyclePermissionMode: permission.cycle,
        sendDisabledReason: unreachable
          ? "Session offline — reconnect to continue"
          : hasPendingOwnElicitation(state.blocks, sessionId)
            ? "Respond to the pending request above to continue"
            : undefined,
        placeholder: "Send a message…",
      }}
    />
  );
}

function SessionWorkspace({ session }: { session: Session }) {
  const isMobile = useIsMobileViewport();
  const context = composerContextFromLabels(session.labels);
  const workspace =
    session.workspace?.trim() ||
    (context.workingDirectory.kind === "selected" ? context.workingDirectory.path : null);
  const git = useComposerGitStatus({
    sessionId: session.id,
    hostId: session.hostId,
    workspace,
    creationBranch: session.gitBranch,
  });
  const state = useConversationEntryState(session.id);
  if (isMobile) return null;
  return (
    <ComposerWorkspaceBar>
      <ComposerWorkspaceStatus
        workspacePath={workspace}
        worktreePath={git.worktreePath}
        isWorktree={git.isWorktree}
        branch={git.branch}
        branchState={git.branchState}
        creationBranch={git.creationBranch}
        showWorktree={git.isWorktree === true}
      />
      <div className="ml-auto">
        <ComposerContextRing contextWindow={state.contextWindow} tokensUsed={state.tokensUsed} />
      </div>
    </ComposerWorkspaceBar>
  );
}
