import { useCallback, useMemo } from "react";

import { useRunnerHealthRegistration } from "./RunnerHealthProvider";
import { useHosts } from "./useHosts";
import { useReleaseHostHeldSends } from "./useReleaseHostHeldSends";
import { useSession } from "./useSession";
import { useSessionReconnect } from "./useSessionReconnect";
import { getCliServerUrl } from "@/lib/host";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { ReconnectSessionDialog } from "@/shell/ReconnectSessionDialog";
import { useChatStore } from "@/store/chatStore";

/** Keep recovery attached to the queued message's session across route changes. */
export function HostSendRecovery() {
  const queuedMessages = useChatStore((s) => s.queuedMessages);
  const sessionIds = useMemo(
    () => [...new Set(queuedMessages.filter((m) => m.waitingForHost).map((m) => m.conversationId))],
    [queuedMessages],
  );
  return sessionIds.map((sessionId) => (
    <SessionHostSendRecovery key={sessionId} sessionId={sessionId} />
  ));
}

function SessionHostSendRecovery({ sessionId }: { sessionId: string }) {
  const needsReconnect = useChatStore((s) =>
    s.queuedMessages.some(
      (m) => m.conversationId === sessionId && m.waitingForHost && !m.hostReconnectAttempted,
    ),
  );
  const markReconnectAttempted = useCallback(() => {
    useChatStore.setState((s) => ({
      queuedMessages: s.queuedMessages.map((m) =>
        m.conversationId === sessionId && m.waitingForHost
          ? { ...m, hostReconnectAttempted: true }
          : m,
      ),
    }));
  }, [sessionId]);
  const { session } = useSession(sessionId);
  const { data: hosts } = useHosts({ includeSandbox: true, enabled: !!session?.hostId });
  const host = hosts?.find((h) => h.host_id === session?.hostId);
  const isOwner = session !== null && isOwnerLevel(session.permissionLevel);
  const { reconnected, dialogOpen, setDialogOpen, localReconnect, arcaReconnect } =
    useSessionReconnect({
      sessionId,
      hostId: session?.hostId ?? null,
      hostName: host?.name,
      isOwner,
      hasHostHeldMessages: needsReconnect,
      onAutomaticReconnect: markReconnectAttempted,
    });
  const healthSessions = useMemo(() => [{ id: sessionId }], [sessionId]);
  useRunnerHealthRegistration(healthSessions);
  useReleaseHostHeldSends(sessionId, reconnected);

  if (!session || !dialogOpen) return null;
  return (
    <ReconnectSessionDialog
      open={dialogOpen}
      onOpenChange={setDialogOpen}
      localReconnect={localReconnect}
      arcaReconnect={arcaReconnect}
      conversationId={sessionId}
      serverUrl={getCliServerUrl()}
      wrapper={session.labels?.["omnigent.wrapper"]}
      harness={session.harness}
      state={session.hostId ? "host_offline" : "local_stranded"}
      isOwner={isOwner}
      sourceTitle={session.title}
      sourceWorkspace={session.workspace}
      sourceHostId={session.hostId}
      sourceGitBranch={session.gitBranch}
    />
  );
}
