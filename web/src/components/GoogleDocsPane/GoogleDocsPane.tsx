import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from "react";
import {
  ExternalLink,
  FilePenLine,
  MoreHorizontal,
  RefreshCw,
  Sparkles,
  Unlink,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { classifyGoogleDocsUrl, type GoogleDocsReference } from "@/lib/googleDocs";

export interface GoogleDocsOpenRequest {
  id: string;
  sessionId: string;
  canonicalUrl: string;
}

interface GoogleDocsPaneProps {
  document: GoogleDocsReference | null;
  savedDocument: GoogleDocsReference | null;
  canEditReference: boolean;
  revisionDisabledReason?: string;
  openRequest?: GoogleDocsOpenRequest;
  onOpenRequestHandled?: (request: GoogleDocsOpenRequest) => void;
  onSave: (url: string | null) => Promise<void>;
  onUseSavedDocument: () => void;
  onRequestRevision: (url: string) => Promise<void>;
}

// The Google origin is needed for the editor's cookies and storage.
const EDITOR_SANDBOX =
  "allow-scripts allow-same-origin allow-forms allow-popups allow-downloads allow-modals";

export function GoogleDocsPane({
  document,
  savedDocument,
  canEditReference,
  revisionDisabledReason,
  openRequest,
  onOpenRequestHandled,
  onSave,
  onUseSavedDocument,
  onRequestRevision,
}: GoogleDocsPaneProps) {
  const [changing, setChanging] = useState(false);
  const [draft, setDraft] = useState("");
  const [validationError, setValidationError] = useState<string>();
  const [failedSave, setFailedSave] = useState<{ url: string | null; message: string }>();
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [reload, setReload] = useState(0);
  const [requesting, setRequesting] = useState(false);
  const [revisionError, setRevisionError] = useState<{ url: string; message: string }>();
  const requestingRef = useRef(false);
  const inputId = useId();
  const validationId = useId();
  const revisionReasonId = useId();
  const displayedUrl = document?.canonicalUrl ?? null;
  const editorUrl = displayedUrl === null ? null : new URL(displayedUrl);
  editorUrl?.searchParams.set("embedded", "true");
  const savedUrl = savedDocument?.canonicalUrl ?? null;
  const handledOpenRequests = useRef(new Set<string>());

  useEffect(() => {
    if (displayedUrl === null) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [displayedUrl]);

  const confirmEditorChange = useCallback(
    (action: string) =>
      displayedUrl === null ||
      window.confirm(
        `${action}? Check that Google Docs has saved your edits and comments before continuing.`,
      ),
    [displayedUrl],
  );

  const confirmUseSavedDocument = useCallback(() => {
    if (
      confirmEditorChange(savedUrl === null ? "Close the editor" : "Switch to the saved document")
    ) {
      onUseSavedDocument();
    }
  }, [confirmEditorChange, onUseSavedDocument, savedUrl]);

  const save = useCallback(
    async (url: string | null, fromOpenRequest = false) => {
      if (!canEditReference || savingRef.current) return;
      if (
        url !== displayedUrl &&
        !confirmEditorChange(url === null ? "Unlink this document" : "Change the document")
      )
        return;
      if (fromOpenRequest) {
        setChanging(true);
        setDraft(url ?? "");
        setValidationError(undefined);
      }
      savingRef.current = true;
      setSaving(true);
      setFailedSave(undefined);
      try {
        await onSave(url);
        setChanging(false);
        setDraft("");
      } catch (error) {
        setFailedSave({
          url,
          message: error instanceof Error ? error.message : "Try again.",
        });
      } finally {
        savingRef.current = false;
        setSaving(false);
      }
    },
    [canEditReference, confirmEditorChange, displayedUrl, onSave],
  );

  useEffect(() => {
    if (
      !openRequest ||
      handledOpenRequests.current.has(openRequest.id) ||
      saving ||
      savingRef.current
    )
      return;
    handledOpenRequests.current.add(openRequest.id);
    onOpenRequestHandled?.(openRequest);
    const url = openRequest.canonicalUrl;
    if (url === displayedUrl) return;
    if (url === savedUrl) {
      confirmUseSavedDocument();
    } else {
      void save(url, true);
    }
  }, [
    openRequest,
    onOpenRequestHandled,
    displayedUrl,
    savedUrl,
    saving,
    confirmUseSavedDocument,
    save,
  ]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canEditReference || savingRef.current) return;
    const link = classifyGoogleDocsUrl(draft);
    if (link.kind === "invalid") {
      setValidationError(link.reason);
    } else if (link.kind === "external") {
      window.open(link.originalUrl.trim(), "_blank", "noopener,noreferrer");
    } else {
      void save(link.reference.canonicalUrl);
    }
  }

  async function requestRevision() {
    if (
      !canEditReference ||
      displayedUrl === null ||
      savingRef.current ||
      revisionDisabledReason ||
      requestingRef.current
    )
      return;
    requestingRef.current = true;
    setRequesting(true);
    setRevisionError(undefined);
    try {
      await onRequestRevision(displayedUrl);
    } catch (error) {
      setRevisionError({
        url: displayedUrl,
        message: error instanceof Error ? error.message : "Try again.",
      });
    } finally {
      requestingRef.current = false;
      setRequesting(false);
    }
  }

  function changeDocument() {
    setDraft(displayedUrl ?? "");
    setValidationError(undefined);
    setChanging(true);
  }

  function reloadEditor() {
    if (confirmEditorChange("Reload the editor")) setReload((count) => count + 1);
  }

  const revisionDisabled = saving || requesting || !!revisionDisabledReason;

  return (
    <section
      aria-label="Google Docs pane"
      className="@container/google-docs flex h-full min-h-0 min-w-0 flex-col"
    >
      {displayedUrl !== null && (
        <div className="flex shrink-0 flex-nowrap items-center gap-1 border-b border-border p-2">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="shrink-0 @min-[36rem]/google-docs:hidden"
                aria-label="Document actions"
              >
                <MoreHorizontal className="size-4" aria-hidden="true" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" aria-label="Document actions">
              {canEditReference && (
                <DropdownMenuItem disabled={saving || changing} onSelect={changeDocument}>
                  <FilePenLine aria-hidden="true" />
                  Change document
                </DropdownMenuItem>
              )}
              <DropdownMenuItem disabled={saving} onSelect={reloadEditor}>
                <RefreshCw aria-hidden="true" />
                Reload
              </DropdownMenuItem>
              <DropdownMenuItem asChild>
                <a href={displayedUrl} target="_blank" rel="noopener noreferrer">
                  <ExternalLink aria-hidden="true" />
                  Open in Google Docs
                </a>
              </DropdownMenuItem>
              {canEditReference && (
                <>
                  <DropdownMenuItem
                    disabled={revisionDisabled}
                    aria-describedby={revisionDisabledReason ? revisionReasonId : undefined}
                    onSelect={() => void requestRevision()}
                  >
                    <Sparkles aria-hidden="true" />
                    Request agent review
                  </DropdownMenuItem>
                  <DropdownMenuItem disabled={saving} onSelect={() => void save(null)}>
                    <Unlink aria-hidden="true" />
                    Unlink document
                  </DropdownMenuItem>
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
          <div className="hidden min-w-0 flex-nowrap items-center gap-1 @min-[36rem]/google-docs:flex">
            {canEditReference && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                disabled={saving || changing}
                onClick={changeDocument}
              >
                Change document
              </Button>
            )}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={saving}
              onClick={reloadEditor}
            >
              Reload
            </Button>
            <Button variant="ghost" size="sm" asChild>
              <a href={displayedUrl} target="_blank" rel="noopener noreferrer">
                Open in Google Docs
              </a>
            </Button>
            {canEditReference && (
              <Button
                type="button"
                size="sm"
                loading={requesting}
                disabled={revisionDisabled}
                title="Requires an authorized Google connection in this session"
                aria-describedby={revisionDisabledReason ? revisionReasonId : undefined}
                onClick={() => void requestRevision()}
              >
                Request agent review
              </Button>
            )}
          </div>
        </div>
      )}
      {revisionDisabledReason && canEditReference && displayedUrl !== null && (
        <p id={revisionReasonId} className="px-3 py-2 text-xs text-muted-foreground">
          {revisionDisabledReason}
        </p>
      )}
      {savedUrl !== displayedUrl && (
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border p-2 text-xs">
          <p>
            {savedUrl === null
              ? "The saved document was unlinked."
              : "The session has a different saved document."}
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={saving}
            onClick={confirmUseSavedDocument}
          >
            {savedUrl === null ? "Close editor" : "Switch to saved document"}
          </Button>
        </div>
      )}
      {canEditReference && (displayedUrl === null || changing) ? (
        <form onSubmit={submit} noValidate className="space-y-2 border-b border-border p-3">
          <label htmlFor={inputId} className="text-ui font-medium">
            Google Docs URL
          </label>
          <Input
            id={inputId}
            type="url"
            value={draft}
            placeholder="https://docs.google.com/document/d/…/edit"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            disabled={saving}
            aria-invalid={!!validationError}
            aria-describedby={validationError ? validationId : undefined}
            onChange={(event) => {
              setDraft(event.target.value);
              setValidationError(undefined);
              setFailedSave(undefined);
            }}
          />
          {validationError && (
            <p id={validationId} role="alert" className="text-xs text-destructive">
              {validationError}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button type="submit" size="sm" loading={saving} disabled={saving || !draft.trim()}>
              Open document
            </Button>
            {displayedUrl !== null && (
              <>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  disabled={saving}
                  onClick={() => {
                    setChanging(false);
                    setFailedSave(undefined);
                  }}
                >
                  Cancel
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={saving}
                  onClick={() => void save(null)}
                >
                  Unlink document
                </Button>
              </>
            )}
          </div>
          {failedSave && (
            <div className="flex flex-wrap items-center gap-2">
              <p role="alert" className="text-xs text-destructive">
                Could not save the document. {failedSave.message}
              </p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={saving}
                onClick={() => void save(failedSave.url)}
              >
                Retry saving document
              </Button>
            </div>
          )}
        </form>
      ) : displayedUrl === null && savedUrl === null ? (
        <p className="p-3 text-sm text-muted-foreground">
          No Google Doc is linked to this session.
        </p>
      ) : null}
      {revisionError && (
        <p role="alert" className="shrink-0 px-3 py-2 text-xs text-destructive">
          {revisionError.url === displayedUrl
            ? "Could not request a review."
            : "Could not request a review for the previous document."}{" "}
          {revisionError.message}
        </p>
      )}
      {editorUrl !== null && (
        <iframe
          key={`${displayedUrl}:${reload}`}
          src={editorUrl.href}
          title="Google Docs editor"
          className="min-h-0 w-full flex-1 border-0 bg-background"
          referrerPolicy="no-referrer"
          sandbox={EDITOR_SANDBOX}
        />
      )}
    </section>
  );
}
