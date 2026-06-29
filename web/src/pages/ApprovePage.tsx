/**
 * Standalone approval page for URL-mode elicitations.
 *
 * Reached via `/approve/:sessionId/:elicitationId` — the URL the
 * REPL displays when a policy returns ASK in URL mode. Fetches the
 * elicitation state from
 * `GET /v1/sessions/{sid}/elicitations/{eid}` and renders
 * approve/reject controls using the same design system as the
 * inline `ApprovalCard`.
 *
 * Three states:
 * - **Loading** — fetching the elicitation.
 * - **Pending** — approve/reject buttons shown.
 * - **Resolved** — the elicitation was already resolved, timed out,
 *   or the id is unknown.
 */

import { useCallback, useEffect, useState } from "react";
import { useParams } from "@/lib/routing";
import { CheckIcon, MessageCircleQuestionMark, XIcon } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { authenticatedFetch } from "@/lib/identity";
import { formatPreview } from "@/lib/previewFormat";

interface ElicitationData {
  status: "pending" | "resolved";
  message?: string;
  phase?: string;
  policy_name?: string;
  content_preview?: string;
}

type PageState =
  | { kind: "loading" }
  | { kind: "pending"; data: ElicitationData }
  | { kind: "resolved" }
  | { kind: "submitted"; action: "accept" | "decline" }
  | { kind: "error"; message: string };

export function ApprovePage() {
  const { sessionId, elicitationId } = useParams<{
    sessionId: string;
    elicitationId: string;
  }>();
  const [state, setState] = useState<PageState>({ kind: "loading" });

  useEffect(() => {
    if (!sessionId || !elicitationId) {
      setState({ kind: "error", message: "Missing session or elicitation ID" });
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const res = await authenticatedFetch(
          `/v1/sessions/${encodeURIComponent(sessionId)}/elicitations/${encodeURIComponent(elicitationId)}`,
        );
        if (cancelled) return;
        if (!res.ok) {
          setState({ kind: "error", message: `Server error: ${res.status}` });
          return;
        }
        const data: ElicitationData = await res.json();
        if (data.status === "resolved") {
          setState({ kind: "resolved" });
        } else {
          setState({ kind: "pending", data });
        }
      } catch (err) {
        if (!cancelled) {
          setState({ kind: "error", message: `Failed to load: ${String(err)}` });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId, elicitationId]);

  const submit = useCallback(
    async (action: "accept" | "decline") => {
      if (!sessionId || !elicitationId) return;
      setState({ kind: "submitted", action });
      try {
        const res = await authenticatedFetch(
          `/v1/sessions/${encodeURIComponent(sessionId)}/elicitations/${encodeURIComponent(elicitationId)}/resolve`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action }),
          },
        );
        if (!res.ok) {
          setState({ kind: "error", message: `Resolve failed: ${res.status}` });
        }
      } catch (err) {
        setState({ kind: "error", message: `Network error: ${String(err)}` });
      }
    },
    [sessionId, elicitationId],
  );

  return (
    <div className="mx-auto flex min-h-screen max-w-xl items-center justify-center p-6">
      {state.kind === "loading" && (
        <Alert className="flex flex-col gap-2 py-4 px-5">
          <AlertTitle className="text-sm">Loading elicitation…</AlertTitle>
        </Alert>
      )}

      {state.kind === "resolved" && (
        <Alert className="flex flex-col gap-2 border-muted py-4 px-5">
          <AlertTitle className="text-sm">Elicitation resolved</AlertTitle>
          <AlertDescription className="text-xs">
            This approval request is no longer pending. It may have been resolved, timed out, or
            cancelled.
          </AlertDescription>
        </Alert>
      )}

      {state.kind === "error" && (
        <Alert variant="destructive" className="flex flex-col gap-2 py-4 px-5">
          <AlertTitle className="text-sm">Error</AlertTitle>
          <AlertDescription className="text-xs">{state.message}</AlertDescription>
        </Alert>
      )}

      {state.kind === "submitted" && (
        <Alert className="flex flex-col gap-1 border-muted py-4 px-5">
          <AlertTitle className="flex items-center gap-2 text-sm">
            {state.action === "accept" ? (
              <>
                <CheckIcon className="size-4 text-success" />
                Approved
              </>
            ) : (
              <>
                <XIcon className="size-4 text-destructive" />
                Rejected
              </>
            )}
          </AlertTitle>
          <AlertDescription className="text-xs">You can close this page.</AlertDescription>
        </Alert>
      )}

      {state.kind === "pending" && (
        <Alert className="flex flex-col gap-3 py-4 px-5">
          <AlertTitle className="flex items-center gap-2 text-sm">
            <MessageCircleQuestionMark className="size-4 text-yellow-600 dark:text-yellow-400" />
            Approval required
            {state.data.policy_name && (
              <span className="text-muted-foreground text-xs">· {state.data.policy_name}</span>
            )}
            {state.data.phase && (
              <span className="text-muted-foreground text-xs">({state.data.phase})</span>
            )}
          </AlertTitle>
          <AlertDescription className="flex flex-col gap-2">
            <span>{state.data.message}</span>
            {state.data.content_preview && (
              <pre className="max-h-64 overflow-y-auto rounded bg-muted px-2 py-1 font-mono text-xs whitespace-pre-wrap break-words">
                {formatPreview(state.data.content_preview)}
              </pre>
            )}
            <div className="flex flex-wrap gap-2 pt-1">
              <Button size="sm" onClick={() => void submit("accept")}>
                <CheckIcon className="mr-1 size-3.5" />
                Approve
              </Button>
              <Button size="sm" variant="outline" onClick={() => void submit("decline")}>
                <XIcon className="mr-1 size-3.5" />
                Reject
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}
    </div>
  );
}
