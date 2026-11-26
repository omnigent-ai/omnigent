import { useCallback, useEffect } from "react";
import { ArrowLeftIcon } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { getProjectFile, OPEN_PROJECT_FILE_EVENT } from "@/lib/projectTicketsApi";
import {
  parseProjectFileContentPath,
  projectFileApiPath,
  projectFileScopeId,
} from "@/lib/projectFileScope";
import { useLocation, useNavigate, useSearchParams } from "@/lib/routing";
import { FileViewer } from "@/shell/FileViewer";
import { TicketLoadError } from "./TicketControls";
import { ApiError } from "@/lib/sessionsApi";

/** The project home's query parameter naming the project file open in the viewer. */
export const PROJECT_FILE_PARAM = "artifact";

/**
 * Opens project files in the session FileViewer, with review comments.
 * Project file links anywhere on the page, and `openProjectFile` calls, open here.
 */
export function ProjectFileViewerHost({ projectId }: { projectId: string | null }) {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const fileId = searchParams.get(PROJECT_FILE_PARAM);
  const backToFileId = projectFileBackTarget(location.state);

  const setFile = useCallback(
    (id: string | null, options: { push: boolean; backTo?: string }) => {
      const next = new URLSearchParams(location.search);
      if (id) next.set(PROJECT_FILE_PARAM, id);
      else next.delete(PROJECT_FILE_PARAM);
      next.delete("comment");
      const state = options.backTo ? { [PROJECT_FILE_BACK_STATE]: options.backTo } : null;
      navigate({ search: next.toString(), hash: location.hash }, { replace: !options.push, state });
    },
    [navigate, location.search, location.hash],
  );
  // A link from one open file to another is a step forward, so Back returns to the first.
  const openFile = useCallback(
    (id: string) => {
      if (id === fileId) return;
      if (fileId) setFile(id, { push: true, backTo: fileId });
      else setFile(id, { push: false });
    },
    [setFile, fileId],
  );
  const close = useCallback(() => setFile(null, { push: false }), [setFile]);
  const back = useCallback(() => navigate(-1), [navigate]);

  useEffect(() => {
    if (!projectId) return;
    const ownFile = (href: string) => {
      const target = parseProjectFileContentPath(href);
      return target?.projectId === projectId ? target.fileId : null;
    };
    function onOpenRequest(event: Event) {
      const id = ownFile((event as CustomEvent<{ contentPath: string }>).detail.contentPath);
      if (!id) return;
      event.preventDefault();
      openFile(id);
    }
    // Markdown links to a file's raw content open the viewer instead.
    function onClick(event: MouseEvent) {
      if (event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const anchor = (event.target as Element | null)?.closest?.("a[href]");
      const id = anchor ? ownFile(anchor.getAttribute("href")!) : null;
      if (!id) return;
      event.preventDefault();
      openFile(id);
    }
    window.addEventListener(OPEN_PROJECT_FILE_EVENT, onOpenRequest);
    document.addEventListener("click", onClick, true);
    return () => {
      window.removeEventListener(OPEN_PROJECT_FILE_EVENT, onOpenRequest);
      document.removeEventListener("click", onClick, true);
    };
  }, [projectId, openFile]);

  if (!projectId || !fileId) return null;
  return (
    <ProjectFileDialog
      projectId={projectId}
      fileId={fileId}
      onClose={close}
      backToFileId={backToFileId}
      onBack={back}
    />
  );
}

const PROJECT_FILE_BACK_STATE = "projectFileBack";

function projectFileBackTarget(state: unknown): string | null {
  const value = (state as Record<string, unknown> | null)?.[PROJECT_FILE_BACK_STATE];
  return typeof value === "string" ? value : null;
}

/** "Back to <file>", shown after following a link from one project file to another. */
function ProjectFileBackBar({
  projectId,
  fileId,
  onBack,
}: {
  projectId: string;
  fileId: string;
  onBack: () => void;
}) {
  const previous = useQuery({
    queryKey: ["project-file", projectId, fileId],
    queryFn: () => getProjectFile(projectFileApiPath({ projectId, fileId })),
    retry: false,
  });
  return (
    <div className="flex shrink-0 items-center border-b px-2 py-1">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex min-w-0 items-center gap-1.5 rounded-md px-2 py-1 text-sm text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        <ArrowLeftIcon className="size-4 shrink-0" aria-hidden />
        <span className="truncate">Back to {previous.data?.title ?? "previous file"}</span>
      </button>
    </div>
  );
}

/** A project file in the session FileViewer, in a dialog; access is checked by the server. */
export function ProjectFileDialog({
  projectId,
  fileId,
  onClose,
  backToFileId = null,
  onBack,
}: {
  projectId: string;
  fileId: string;
  onClose: () => void;
  backToFileId?: string | null;
  onBack?: () => void;
}) {
  const file = useQuery({
    queryKey: ["project-file", projectId, fileId],
    queryFn: () => getProjectFile(projectFileApiPath({ projectId, fileId })),
    retry: false,
  });
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        showCloseButton={false}
        aria-describedby={undefined}
        className="flex h-[85vh] max-h-[85vh] w-[min(72rem,95vw)] max-w-none flex-col gap-0 overflow-hidden p-0 sm:max-w-none"
      >
        <DialogTitle className="sr-only">{file.data?.title ?? "Project file"}</DialogTitle>
        {backToFileId && onBack ? (
          <ProjectFileBackBar projectId={projectId} fileId={backToFileId} onBack={onBack} />
        ) : null}
        {file.data ? (
          <FileViewer
            open
            frameless
            syncUrl
            agentId={null}
            conversationId={projectFileScopeId(projectId, fileId)}
            path={file.data.filename}
            onClose={onClose}
            onCloseTab={onClose}
          />
        ) : file.error ? (
          <TicketLoadError
            error={file.error}
            message={projectFileErrorText(file.error)}
            className="m-4"
          />
        ) : (
          <div className="flex flex-1 items-center justify-center">
            <Spinner />
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** A missing project reads the same as no access, so say both. */
export function projectFileErrorText(error: unknown): string {
  const status = error instanceof ApiError ? error.status : null;
  if (status === 403 || status === 404)
    return "You don't have access to this project's files, or the file was removed.";
  return error instanceof Error ? error.message : "Couldn't open the file.";
}
