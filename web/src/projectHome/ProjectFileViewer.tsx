import { useCallback, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { getProjectFile, OPEN_PROJECT_FILE_EVENT } from "@/lib/projectTicketsApi";
import {
  parseProjectFileContentPath,
  projectFileApiPath,
  projectFileScopeId,
} from "@/lib/projectFileScope";
import { useReplaceSearchParams, useSearchParams } from "@/lib/routing";
import { FileViewer } from "@/shell/FileViewer";

/** The project home's query parameter naming the project file open in the viewer. */
export const PROJECT_FILE_PARAM = "artifact";

/**
 * Opens project files in the session FileViewer, with review comments.
 * Project file links anywhere on the page, and `openProjectFile` calls, open here.
 */
export function ProjectFileViewerHost({ projectId }: { projectId: string | null }) {
  const [searchParams] = useSearchParams();
  const replaceSearchParams = useReplaceSearchParams();
  const fileId = searchParams.get(PROJECT_FILE_PARAM);

  const openFile = useCallback(
    (id: string) => {
      const next = new URLSearchParams(window.location.search);
      next.set(PROJECT_FILE_PARAM, id);
      next.delete("comment");
      replaceSearchParams(next);
    },
    [replaceSearchParams],
  );
  const close = useCallback(() => {
    const next = new URLSearchParams(window.location.search);
    next.delete(PROJECT_FILE_PARAM);
    next.delete("comment");
    replaceSearchParams(next);
  }, [replaceSearchParams]);

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

  const file = useQuery({
    queryKey: ["project-file", projectId, fileId],
    queryFn: () => getProjectFile(projectFileApiPath({ projectId: projectId!, fileId: fileId! })),
    enabled: projectId !== null && fileId !== null,
  });

  if (!projectId || !fileId) return null;
  return (
    <Dialog open onOpenChange={(open) => !open && close()}>
      <DialogContent
        showCloseButton={false}
        aria-describedby={undefined}
        className="flex h-[85vh] max-h-[85vh] w-[min(72rem,95vw)] max-w-none flex-col gap-0 overflow-hidden p-0 sm:max-w-none"
      >
        <DialogTitle className="sr-only">{file.data?.title ?? "Project file"}</DialogTitle>
        {file.data ? (
          <FileViewer
            open
            frameless
            syncUrl
            agentId={null}
            conversationId={projectFileScopeId(projectId, fileId)}
            path={file.data.filename}
            onClose={close}
            onCloseTab={close}
          />
        ) : file.error ? (
          <p className="p-4 text-sm text-destructive">{file.error.message}</p>
        ) : (
          <div className="flex flex-1 items-center justify-center">
            <Spinner />
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
