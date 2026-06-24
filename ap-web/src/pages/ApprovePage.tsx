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
import { useTranslation } from "react-i18next";
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
  const { t } = useTranslation("common");
  const { sessionId, elicitationId } = useParams<{
    sessionId: string;
    elicitationId: string;
  }>();
  const [state, setState] = useState<PageState>({ kind: "loading" });

  useEffect(() => {
    if (!sessionId || !elicitationId) {
      setState({ kind: "error", message: t("missingSessionOrElicitationId") });
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
          setState({ kind: "error", message: t("serverError", { status: res.status }) });
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
          setState({ kind: "error", message: `${t("failedToLoad")} ${String(err)}` });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId, elicitationId, t]);

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
          setState({ kind: "error", message: t("resolveFailed", { status: res.status }) });
        }
      } catch (err) {
        setState({ kind: "error", message: t("networkError", { error: String(err) }) });
      }
    },
    [sessionId, elicitationId, t],
  );

  return (
    <div className="mx-auto flex min-h-screen max-w-xl items-center justify-center p-6">
      {state.kind === "loading" && (
        <Alert className="flex flex-col gap-2 py-4 px-5">
          <AlertTitle className="text-sm">{t("loadingElicitation")}</AlertTitle>
        </Alert>
      )}

      {state.kind === "resolved" && (
        <Alert className="flex flex-col gap-2 border-muted py-4 px-5">
          <AlertTitle className="text-sm">{t("elicitationResolved")}</AlertTitle>
          <AlertDescription className="text-xs">{t("elicitationResolvedDesc")}</AlertDescription>
        </Alert>
      )}

      {state.kind === "error" && (
        <Alert variant="destructive" className="flex flex-col gap-2 py-4 px-5">
          <AlertTitle className="text-sm">{t("error")}</AlertTitle>
          <AlertDescription className="text-xs">{state.message}</AlertDescription>
        </Alert>
      )}

      {state.kind === "submitted" && (
        <Alert className="flex flex-col gap-1 border-muted py-4 px-5">
          <AlertTitle className="flex items-center gap-2 text-sm">
            {state.action === "accept" ? (
              <>
                <CheckIcon className="size-4 text-success" />
                {t("approved")}
              </>
            ) : (
              <>
                <XIcon className="size-4 text-destructive" />
                {t("rejected")}
              </>
            )}
          </AlertTitle>
          <AlertDescription className="text-xs">{t("canClosePage")}</AlertDescription>
        </Alert>
      )}

      {state.kind === "pending" && (
        <Alert className="flex flex-col gap-3 py-4 px-5">
          <AlertTitle className="flex items-center gap-2 text-sm">
            <MessageCircleQuestionMark className="size-4 text-yellow-600 dark:text-yellow-400" />
            {t("approvalRequired")}
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
                {t("approve")}
              </Button>
              <Button size="sm" variant="outline" onClick={() => void submit("decline")}>
                <XIcon className="mr-1 size-3.5" />
                {t("reject")}
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}
    </div>
  );
}
