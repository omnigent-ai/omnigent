import { type ReactNode, useState } from "react";
import { useGoogleDocsSession } from "@/shell/GoogleDocsSessionContext";
import { classifyGoogleDocsUrl } from "@/lib/googleDocs";

export function GoogleDocsChatLink({
  url,
  sessionId,
  children,
}: {
  url: string | undefined;
  sessionId: string | undefined;
  children: ReactNode;
}) {
  const state = useGoogleDocsSession();
  const [attempted, setAttempted] = useState(false);
  const membership =
    url && state && state.sessionId === sessionId && !state.loading && !state.loadError
      ? state.membership(url)
      : "unknown";
  const supported = url && classifyGoogleDocsUrl(url).kind === "embed";
  const action =
    supported && membership === "linked"
      ? "Open in Docs pane"
      : supported && membership === "unlinked" && state?.canEdit
        ? "Add to session"
        : null;
  return (
    <>
      {children}
      {action && state && url && (
        <button
          type="button"
          className="ml-1 inline-flex cursor-pointer items-center rounded border border-border px-1.5 text-xs text-muted-foreground hover:bg-muted disabled:opacity-50"
          disabled={state.busy}
          onClick={(event) => {
            if (
              event.button !== 0 ||
              event.metaKey ||
              event.ctrlKey ||
              event.shiftKey ||
              event.altKey ||
              !state.isCurrent() ||
              state.loading
            )
              return;
            if (state.membership(url) === "linked") state.openLinked(url);
            else if (state.membership(url) === "unlinked" && state.canEdit) {
              setAttempted(true);
              void state.addAndOpen(url);
            }
          }}
        >
          {action === "Add to session" && <span aria-hidden="true">＋ </span>}
          {action}
        </button>
      )}
      {attempted && state?.error && !state.busy && (
        <span role="alert" className="ml-1 text-xs text-destructive">
          {state.error}
        </span>
      )}
    </>
  );
}
