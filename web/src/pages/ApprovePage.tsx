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
 * Main states:
 * - **Loading** — fetching the elicitation.
 * - **Pending** — approve/reject buttons shown.
 * - **Recovering** — native delivery is unconfirmed; keep checking for the request.
 * - **Resolved** — the elicitation was already resolved, timed out,
 *   or the id is unknown.
 */

import { ApprovalCard } from "@/components/blocks/ApprovalCard";
import {
  parseNativePermissionDelivery,
  type NativePermissionDelivery,
} from "@/lib/nativePermissionDelivery";
import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "@/lib/routing";
import { CheckIcon, MessageCircleQuestionMark, XIcon } from "lucide-react";
import { ElicitationMessage } from "@/components/blocks/ElicitationMessage";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { authenticatedFetch } from "@/lib/identity";
import { formatPreview } from "@/lib/previewFormat";

interface ElicitationData {
  status: "pending" | "resolved";
  resolution?: "unanswered" | "confirmed";
  message?: string;
  phase?: string;
  policy_name?: string;
  content_preview?: string;
  native_delivery?: NativePermissionDelivery;
  permission_directories?: string[];
}

type PageState =
  | { kind: "loading" }
  | { kind: "pending"; data: ElicitationData }
  | { kind: "resolved" }
  | { kind: "recovering" }
  | { kind: "submitted"; action: "accept" | "decline" | "cancel" }
  | { kind: "error"; message: string };

export function ApprovePage() {
  const { sessionId, elicitationId } = useParams<{
    sessionId: string;
    elicitationId: string;
  }>();
  const [state, setState] = useState<PageState>({ kind: "loading" });
  const submission = useRef<{ revision: number; pending: boolean; attempt?: string }>({
    revision: 0,
    pending: false,
  });

  useEffect(() => {
    if (!sessionId || !elicitationId) {
      setState({ kind: "error", message: "Missing session or elicitation ID" });
      return;
    }
    let cancelled = false;
    let polling = false;
    let nativeDeliverySeen = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      const revision = submission.current.revision;
      try {
        const res = await authenticatedFetch(
          `/v1/sessions/${encodeURIComponent(sessionId)}/elicitations/${encodeURIComponent(elicitationId)}`,
        );
        if (cancelled) return;
        if (!res.ok) {
          if (polling || revision !== submission.current.revision) return;
          setState({ kind: "error", message: `Server error: ${res.status}` });
          return;
        }
        const data: ElicitationData = await res.json();
        if (cancelled) return;
        data.native_delivery = parseNativePermissionDelivery(data.native_delivery);
        nativeDeliverySeen ||= Boolean(data.native_delivery) || data.resolution === "unanswered";
        polling =
          (nativeDeliverySeen && data.resolution !== "confirmed") ||
          (data.status !== "resolved" && revision !== submission.current.revision);
        if (data.status === "resolved") {
          submission.current = { revision: submission.current.revision + 1, pending: false };
          setState({ kind: polling ? "recovering" : "resolved" });
        } else if (revision === submission.current.revision) {
          // A new server attempt supersedes the POST still waiting on its response.
          if (
            data.native_delivery &&
            submission.current.attempt &&
            data.native_delivery.attempt !== submission.current.attempt
          ) {
            submission.current = { revision: revision + 1, pending: false };
          }
          if (!submission.current.pending) setState({ kind: "pending", data });
        }
      } catch (err) {
        if (!cancelled && !polling && revision === submission.current.revision) {
          setState({ kind: "error", message: `Failed to load: ${String(err)}` });
        }
      } finally {
        if (!cancelled && polling) timer = setTimeout(() => void load(), 1000);
      }
    };
    void load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [sessionId, elicitationId]);

  const submit = useCallback(
    async (
      action: "accept" | "decline" | "cancel",
      content?: Record<string, unknown>,
      meta?: Record<string, unknown>,
    ) => {
      if (!sessionId || !elicitationId || submission.current.pending) return;
      const submitted = {
        revision: submission.current.revision + 1,
        pending: true,
        attempt: state.kind === "pending" ? state.data.native_delivery?.attempt : undefined,
      };
      submission.current = submitted;
      if (state.kind === "pending" && state.data.native_delivery) {
        setState({
          kind: "pending",
          data: {
            ...state.data,
            native_delivery: { ...state.data.native_delivery, state: "sending", action },
          },
        });
      } else {
        setState({ kind: "submitted", action });
      }
      try {
        const res = await authenticatedFetch(
          `/v1/sessions/${encodeURIComponent(sessionId)}/elicitations/${encodeURIComponent(elicitationId)}/resolve`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              action,
              ...(content ? { content } : {}),
              ...(meta ? { _meta: meta } : {}),
            }),
          },
        );
        if (!res.ok && submission.current === submitted) {
          setState({ kind: "error", message: `Resolve failed: ${res.status}` });
        }
      } catch (err) {
        if (submission.current === submitted) {
          setState({ kind: "error", message: `Network error: ${String(err)}` });
        }
      } finally {
        submitted.pending = false;
      }
    },
    [sessionId, elicitationId, state],
  );

  return (
    <div className="mx-auto flex min-h-screen max-w-xl items-center justify-center p-6">
      {state.kind === "loading" && (
        <Alert className="flex flex-col gap-2 py-4 px-5">
          <AlertTitle className="text-ui">Loading elicitation…</AlertTitle>
        </Alert>
      )}

      {state.kind === "recovering" && (
        <Alert className="flex flex-col gap-2 py-4 px-5">
          <AlertTitle className="text-ui">Delivery unconfirmed</AlertTitle>
          <AlertDescription className="text-sm">
            The approval request is temporarily unavailable. Checking for recovery…
          </AlertDescription>
        </Alert>
      )}

      {state.kind === "resolved" && (
        <Alert className="flex flex-col gap-2 border-muted py-4 px-5">
          <AlertTitle className="text-ui">Elicitation resolved</AlertTitle>
          <AlertDescription className="text-sm">
            This approval request is no longer pending. It may have been resolved, timed out, or
            cancelled.
          </AlertDescription>
        </Alert>
      )}

      {state.kind === "error" && (
        <Alert variant="destructive" className="flex flex-col gap-2 py-4 px-5">
          <AlertTitle className="text-ui">Error</AlertTitle>
          <AlertDescription className="text-sm">{state.message}</AlertDescription>
        </Alert>
      )}

      {state.kind === "submitted" && (
        <Alert className="flex flex-col gap-1 border-muted py-4 px-5">
          <AlertTitle className="flex items-center gap-2 text-ui">
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
          <AlertDescription className="text-sm">You can close this page.</AlertDescription>
        </Alert>
      )}

      {state.kind === "pending" &&
        (state.data.native_delivery || Boolean(state.data.permission_directories?.length)) && (
          <ApprovalCard
            elicitationId={elicitationId!}
            message={state.data.message ?? ""}
            phase={state.data.phase ?? ""}
            policyName={state.data.policy_name ?? ""}
            contentPreview={state.data.content_preview ?? ""}
            requestedSchema={{}}
            status="pending"
            response={null}
            nativeDelivery={state.data.native_delivery}
            permissionDirectories={state.data.permission_directories}
            onSubmit={(_id, action, content, meta) => void submit(action, content, meta)}
          />
        )}
      {state.kind === "pending" &&
        !state.data.native_delivery &&
        !state.data.permission_directories?.length && (
          <Alert className="flex flex-col gap-3 py-4 px-5">
            <AlertTitle className="flex items-center gap-2 text-ui">
              <MessageCircleQuestionMark className="size-4 text-yellow-600 dark:text-yellow-400" />
              Approval required
              {state.data.policy_name && (
                <span className="text-muted-foreground text-sm">· {state.data.policy_name}</span>
              )}
              {state.data.phase && (
                <span className="text-muted-foreground text-sm">({state.data.phase})</span>
              )}
            </AlertTitle>
            <AlertDescription className="flex flex-col gap-2">
              <ElicitationMessage message={state.data.message ?? ""} />
              {state.data.content_preview && (
                <pre className="max-h-64 overflow-y-auto rounded bg-muted px-2 py-1 font-mono text-sm whitespace-pre-wrap break-words">
                  {formatPreview(state.data.content_preview)}
                </pre>
              )}
              <div className="flex flex-wrap gap-2 pt-1">
                <Button
                  size="sm"
                  onClick={() => void submit("accept")}
                  componentId="approve.approve"
                >
                  <CheckIcon className="mr-1 size-3.5" />
                  Approve
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void submit("decline")}
                  componentId="approve.reject"
                >
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
