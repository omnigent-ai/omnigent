import { useQuery } from "@tanstack/react-query";
import { ErrorBanner } from "@/components/blocks/StatusBlocks";
import { getSessionSignInLink } from "@/lib/sessionsApi";

/** How often an empty native session asks its host whether the agent pane awaits a sign-in. */
export const PENDING_SIGN_IN_POLL_MS = 5_000;

/** Offer the sign-in a native agent pane is waiting on before the first message fails on it. */
export function PendingSignInNotice({ sessionId }: { sessionId: string }) {
  const { data } = useQuery({
    queryKey: ["session-sign-in-link", sessionId],
    queryFn: ({ signal }) => getSessionSignInLink(sessionId, { signal }),
    refetchInterval: PENDING_SIGN_IN_POLL_MS,
    retry: false,
  });
  if (!data?.pending) return null;
  return (
    <ErrorBanner
      message="The agent is waiting for a sign-in in this session's terminal."
      source="harness"
      code="databricks_sign_in_pending"
      title="Sign in to Databricks to start the agent"
      level="info"
    />
  );
}
