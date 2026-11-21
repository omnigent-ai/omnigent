import { useCallback, useEffect, useRef, useState } from "react";
import {
  GoogleDocsPane,
  type GoogleDocsOpenRequest,
} from "@/components/GoogleDocsPane/GoogleDocsPane";
import { Button } from "@/components/ui/button";
import { readGoogleDocsLabels, type GoogleDocsReference } from "@/lib/googleDocs";
import {
  loadGoogleDocsSession,
  saveGoogleDocsSession,
  type GoogleDocsSessionRequest,
} from "@/lib/googleDocsSession";
import { isEditorLevel } from "@/lib/permissionsApi";
import { sessionUpdatesSocket } from "@/lib/sessionUpdatesSocket";
import { useChatStore } from "@/store/chatStore";
import { requestGoogleDocRevision } from "@/lib/googleDocsRequests";

export type { GoogleDocsOpenRequest };

export interface GoogleDocsWorkspacePaneProps {
  sessionId: string;
  active: boolean;
  visible: boolean;
  openRequest?: GoogleDocsOpenRequest;
  onOpenRequestHandled?: (request: GoogleDocsOpenRequest) => void;
  canEditReference: boolean;
  revisionDisabledReason?: string;
}

interface DocumentView {
  displayed: GoogleDocsReference | null;
  saved: GoogleDocsReference | null;
}

function SessionGoogleDocsPane({
  sessionId,
  active,
  visible,
  canEditReference,
  revisionDisabledReason,
  openRequest,
  onOpenRequestHandled,
}: GoogleDocsWorkspacePaneProps) {
  const [view, setView] = useState<DocumentView>();
  const [loadError, setLoadError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [streamedEditAccess, setStreamedEditAccess] = useState<boolean>();
  const canEdit = active && (streamedEditAccess ?? canEditReference);
  const mounted = useRef(false);
  const latestRead = useRef<GoogleDocsSessionRequest | undefined>(undefined);
  const latestSave = useRef<GoogleDocsSessionRequest | undefined>(undefined);
  const chatReady = useChatStore(
    (chat) => chat.conversationId === sessionId && chat.boundAgentId !== null,
  );

  const load = useCallback(async () => {
    const request = loadGoogleDocsSession(sessionId);
    latestRead.current = request;
    const isCurrent = () =>
      mounted.current &&
      request.sessionId === sessionId &&
      request.generation === latestRead.current?.generation &&
      request.generation > (latestSave.current?.generation ?? 0);
    setLoading(true);
    try {
      const { selected } = await request.result;
      if (!isCurrent()) return;
      setView((previous) =>
        previous === undefined || previous.displayed === null
          ? { displayed: selected, saved: selected }
          : { ...previous, saved: selected },
      );
      setLoadError(undefined);
    } catch (error) {
      if (isCurrent()) setLoadError(error instanceof Error ? error.message : "Try again.");
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      latestRead.current = undefined;
      latestSave.current = undefined;
    };
  }, []);

  useEffect(() => {
    if (!active) {
      setStreamedEditAccess(undefined);
      return;
    }
    let lastStreamedUrl: string | null | undefined;
    const unsubscribe = sessionUpdatesSocket.subscribe((frame) => {
      if (frame.type === "snapshot" || frame.type === "changed") {
        const item = frame.items.find((candidate) => candidate.id === sessionId);
        if (!item) return;
        if (typeof item.permission_level === "number") {
          setStreamedEditAccess(isEditorLevel(item.permission_level));
        }
        if (!item.labels) return;
        const url = readGoogleDocsLabels(item.labels).selected?.canonicalUrl ?? null;
        if (url === lastStreamedUrl) return;
        lastStreamedUrl = url;
      } else if (
        (frame.type !== "access_changed" && frame.type !== "removed") ||
        !frame.ids.includes(sessionId)
      )
        return;
      if (frame.type === "removed") setStreamedEditAccess(false);
      void load();
    });
    void load();
    return unsubscribe;
  }, [active, sessionId, load]);

  async function save(url: string | null) {
    if (!canEdit) throw new Error("This session is read-only.");
    const request = saveGoogleDocsSession(sessionId, url);
    latestSave.current = request;
    try {
      const { selected } = await request.result;
      if (
        mounted.current &&
        request.sessionId === sessionId &&
        request.generation === latestSave.current?.generation
      ) {
        setView({ displayed: selected, saved: selected });
        setLoadError(undefined);
      }
    } finally {
      if (
        mounted.current &&
        request.generation === latestSave.current?.generation &&
        request.generation > (latestRead.current?.generation ?? 0)
      )
        setLoading(false);
    }
  }

  const disabledReason =
    revisionDisabledReason ??
    (!chatReady ? "Wait for this chat to connect before sending a document request." : undefined);

  return (
    <>
      {loadError && (
        <div className="flex shrink-0 flex-wrap items-center gap-2 px-3 py-2">
          <p role="alert" className="text-xs text-destructive">
            Could not load the saved document. {loadError}
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={loading}
            onClick={() => void load()}
          >
            Retry loading document
          </Button>
        </div>
      )}
      {view ? (
        <div className="min-h-0 flex-1">
          <GoogleDocsPane
            document={view.displayed}
            savedDocument={view.saved}
            canEditReference={canEdit}
            revisionDisabledReason={disabledReason}
            openRequest={
              active && visible && openRequest?.sessionId === sessionId ? openRequest : undefined
            }
            onOpenRequestHandled={onOpenRequestHandled}
            onSave={save}
            onUseSavedDocument={() =>
              setView((previous) => previous && { ...previous, displayed: previous.saved })
            }
            onRequestRevision={(url) => {
              if (!canEdit || disabledReason) {
                throw new Error(disabledReason ?? "This session is read-only.");
              }
              return requestGoogleDocRevision(sessionId, url);
            }}
          />
        </div>
      ) : (
        !loadError && (
          <p role="status" className="p-3 text-sm text-muted-foreground">
            Loading saved document…
          </p>
        )
      )}
    </>
  );
}

export function GoogleDocsWorkspacePane(props: GoogleDocsWorkspacePaneProps) {
  return (
    <div
      hidden={!props.visible}
      className={props.visible ? "flex min-h-0 flex-1 flex-col" : undefined}
    >
      <SessionGoogleDocsPane key={props.sessionId} {...props} />
    </div>
  );
}
