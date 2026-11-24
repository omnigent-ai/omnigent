import { useEffect, useId, useRef, useState } from "react";
import { ExternalLink, MoreHorizontal, Plus, Trash2 } from "lucide-react";
import { useGoogleDocsEditorLifecycle } from "@/components/GoogleDocsPane/editorLifecycle";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { classifyGoogleDocsUrl } from "@/lib/googleDocs";
import { requestGoogleDocReview } from "@/lib/googleDocsRequests";
import { GoogleDocsSessionProvider, useGoogleDocsSession } from "./GoogleDocsSessionContext";
import type { LeaveGuard } from "./workspaceTabs/workspaceTabsSessions";

export interface GoogleDocsWorkspacePaneProps {
  sessionId: string;
  visible?: boolean;
  permissionLevel?: number | null;
  registerLeaveGuard?: (guard: LeaveGuard) => () => void;
}
const EDITOR_SANDBOX =
  "allow-scripts allow-same-origin allow-forms allow-popups allow-downloads allow-modals";

function PaneContent({ visible = true, registerLeaveGuard }: GoogleDocsWorkspacePaneProps) {
  const state = useGoogleDocsSession()!;
  const { displayed, entries, busy, canEdit, registerEditor } = state;
  const editorOpen = displayed !== null;
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  const inputId = useId();
  const latest = useRef(state);
  latest.current = state;
  const reviewing = useRef(false);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);
  async function requestReview() {
    if (!displayed || !canEdit || reviewing.current || !state.isCurrent()) return;
    const captured = state;
    const current = () => captured.isCurrent() && latest.current.canEdit;
    reviewing.current = true;
    setReviewBusy(true);
    setReviewError(null);
    try {
      await requestGoogleDocReview(state.sessionId, displayed.originalUrl, 2, current);
    } catch (reason) {
      if (current())
        setReviewError(reason instanceof Error ? reason.message : "Could not request review.");
    } finally {
      reviewing.current = false;
      if (captured.isCurrent()) setReviewBusy(false);
    }
  }
  useGoogleDocsEditorLifecycle(displayed !== null, state.sessionId);
  useEffect(() => (editorOpen ? registerEditor() : undefined), [editorOpen, registerEditor]);
  useEffect(() => {
    if (!editorOpen || !registerLeaveGuard) return;
    return registerLeaveGuard(async () =>
      window.confirm(
        "Close or replace the document editor? Check that Google Docs has saved your edits and comments before continuing.",
      )
        ? "leave"
        : "cancel",
    );
  }, [editorOpen, registerLeaveGuard]);
  const active = entries.find((entry) => {
    const link = classifyGoogleDocsUrl(entry.documentUrl);
    return link.kind === "embed" && link.reference.documentId === displayed?.documentId;
  });
  const editorUrl = displayed ? new URL(displayed.canonicalUrl) : null;
  editorUrl?.searchParams.set("embedded", "true");
  const external = displayed && (
    <a href={displayed.originalUrl} target="_blank" rel="noopener noreferrer">
      Open in Google Docs
    </a>
  );
  return (
    <section
      hidden={!visible}
      aria-label="Google Docs pane"
      className={visible ? "@container/google-docs flex h-full min-h-0 flex-1 flex-col" : "hidden"}
      onFocusCapture={(event) => {
        if (
          event.currentTarget.contains(event.target) &&
          !event.currentTarget.contains(event.relatedTarget)
        )
          state.refresh();
      }}
    >
      <div className="flex shrink-0 items-center gap-1 border-b border-border p-2">
        <Select
          value={active?.documentUrl ?? (displayed ? "unlinked" : "")}
          onOpenChange={(open) => {
            if (open) state.refresh();
          }}
          onValueChange={state.openLinked}
          disabled={busy}
        >
          <SelectTrigger size="sm" aria-label="Session document" className="min-w-0 flex-1">
            <SelectValue placeholder="Select a document" />
          </SelectTrigger>
          <SelectContent>
            {displayed && !active && (
              <SelectItem value="unlinked" disabled>
                {displayed.originalUrl} (not linked)
              </SelectItem>
            )}
            {entries.map((entry) => (
              <SelectItem key={entry.documentUrl} value={entry.documentUrl}>
                {entry.documentUrl}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Add document"
          title={
            canEdit ? "Add document to this shared session" : "Session edit access is required"
          }
          disabled={!canEdit || busy}
          onClick={() => setAdding((open) => !open)}
        >
          <Plus />
        </Button>
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Remove document"
          title="Remove from this shared session only; the Google Doc is not deleted"
          disabled={!canEdit || busy || !active}
          onClick={() => {
            if (active) void state.remove(active);
          }}
        >
          <Trash2 />
        </Button>
        {external && (
          <>
            <Button
              size="sm"
              variant="ghost"
              asChild
              className="hidden @min-[36rem]/google-docs:inline-flex"
            >
              <a href={displayed.originalUrl} target="_blank" rel="noopener noreferrer">
                <ExternalLink />
                Open in Google Docs
              </a>
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="hidden @min-[36rem]/google-docs:inline-flex"
              disabled={!canEdit || busy || reviewBusy}
              title={
                !canEdit ? "Session edit access is required to request agent review" : undefined
              }
              onClick={() => void requestReview()}
            >
              Request agent review
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label="Document actions"
                  className="@min-[36rem]/google-docs:hidden"
                >
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  disabled={!canEdit || busy || reviewBusy}
                  onSelect={() => void requestReview()}
                >
                  Request agent review
                </DropdownMenuItem>
                <DropdownMenuItem asChild>{external}</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </>
        )}
      </div>
      {reviewError && (
        <p role="alert" className="p-2 text-sm text-destructive">
          {reviewError}
        </p>
      )}
      {(state.loadError || state.error) && (
        <div role="alert" className="p-2 text-sm text-destructive">
          {state.error ?? state.loadError}{" "}
          <Button size="sm" variant="ghost" disabled={busy || state.loading} onClick={state.retry}>
            Retry
          </Button>
        </div>
      )}
      {state.loading && (
        <p role="status" className="px-3 py-1 text-xs text-muted-foreground">
          Loading session documents…
        </p>
      )}
      {displayed && !active && (
        <p role="status" className="px-3 py-1 text-xs text-muted-foreground">
          This document is no longer linked to this session. The editor stays open.
        </p>
      )}
      {(adding || (!displayed && entries.length === 0)) && (
        <form
          className="flex flex-wrap gap-2 border-b border-border p-3"
          onSubmit={(event) => {
            event.preventDefault();
            void state.addAndOpen(draft).then((opened) => {
              if (opened) {
                setAdding(false);
                setDraft("");
              }
            });
          }}
        >
          <label htmlFor={inputId} className="w-full text-sm">
            Google Docs URL
          </label>
          <Input
            id={inputId}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            className="min-w-0 flex-1"
            disabled={!canEdit || busy}
          />
          <Button type="submit" size="sm" disabled={!canEdit || busy || !draft.trim()}>
            Add to session
          </Button>
          <p className="w-full text-xs text-muted-foreground">
            Linked URLs are visible to session participants. Google controls document access.
          </p>
        </form>
      )}
      {editorUrl && (
        <iframe
          data-google-docs-editor=""
          key={displayed?.canonicalUrl}
          title="Google Docs editor"
          src={editorUrl.toString()}
          className="min-h-0 w-full flex-1 border-0"
          sandbox={EDITOR_SANDBOX}
          referrerPolicy="no-referrer"
          allow="clipboard-read; clipboard-write"
        />
      )}
      {!displayed && !state.loading && (
        <p className="p-3 text-sm text-muted-foreground">
          {canEdit
            ? "Select a linked document or add a Google Docs URL."
            : "No document is open. Select a document linked to this session."}
        </p>
      )}
    </section>
  );
}

function Pane(props: GoogleDocsWorkspacePaneProps) {
  const state = useGoogleDocsSession();
  return state ? (
    <PaneContent key={state.scope} {...props} />
  ) : (
    <p role="alert">Session documents are unavailable.</p>
  );
}

export function GoogleDocsWorkspacePane(props: GoogleDocsWorkspacePaneProps) {
  const state = useGoogleDocsSession();
  if (state?.sessionId === props.sessionId) return <Pane {...props} />;
  return (
    <GoogleDocsSessionProvider
      sessionId={props.sessionId}
      permissionLevel={props.permissionLevel === undefined ? 1 : props.permissionLevel}
    >
      <Pane {...props} />
    </GoogleDocsSessionProvider>
  );
}
