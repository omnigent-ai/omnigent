import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { FolderOpenIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Spinner } from "@/components/ui/spinner";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { listProjectFiles, openProjectFile, type ProjectFile } from "@/lib/projectTicketsApi";
import { absoluteTime, formatUploadTime } from "@/lib/relativeTime";
import { Link, useSearchParams } from "@/lib/routing";
import { WorkspaceFileIcon } from "@/shell/WorkspaceFileIcon";
import { ticketPanelSearch } from "./projectHomeNavigation";
import { TicketLoadError } from "./TicketControls";

/** Documents, screenshots and evidence that agents and the owner saved to the project. */
export function ProjectFiles({ projectId }: { projectId: string | null }) {
  const isMobile = useIsMobileViewport();
  const [open, setOpen] = useState(false);
  const files = useQuery({
    queryKey: ["project-files", projectId],
    queryFn: () => listProjectFiles(projectId!),
    enabled: open && projectId !== null,
  });
  if (projectId === null) return null;
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="ghost" size={isMobile ? "icon" : "sm"} aria-label="Files">
          <FolderOpenIcon />
          {!isMobile && <span className="@max-[60rem]/topbar:hidden">Files</span>}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-96 p-0">
        <div className="border-b border-border/60 px-3 py-2 text-sm font-semibold">
          Project files
        </div>
        {files.isLoading ? (
          <div className="flex justify-center p-4">
            <Spinner />
          </div>
        ) : files.error && !files.data ? (
          <TicketLoadError error={files.error} className="m-3" />
        ) : files.data?.length ? (
          <ul className="max-h-96 divide-y divide-border/60 overflow-y-auto">
            {newestFirst(files.data).map((file) => (
              <ProjectFileRow key={file.id} file={file} />
            ))}
          </ul>
        ) : (
          <p className="p-3 text-sm text-muted-foreground">
            No files yet. Agents save documents and evidence here with sys_project_artifact_save.
          </p>
        )}
      </PopoverContent>
    </Popover>
  );
}

function ProjectFileRow({ file }: { file: ProjectFile }) {
  const [search] = useSearchParams();
  const [error, setError] = useState<string | null>(null);
  return (
    <li className="flex items-center gap-3 p-3">
      <WorkspaceFileIcon path={file.filename} mimeType={file.content_type} className="size-4" />
      <button
        type="button"
        className="min-w-0 flex-1 text-left"
        onClick={() => {
          setError(null);
          openProjectFile(file.url, file.filename).catch((reason: unknown) =>
            setError(reason instanceof Error ? reason.message : "Couldn't open the file"),
          );
        }}
      >
        <span className="block truncate text-sm font-medium hover:underline">{file.title}</span>
        <span className="block text-xs text-muted-foreground">
          {error ?? <FileUploadTime createdAt={file.created_at} />}
        </span>
      </button>
      {file.ticket_id && (
        <Link
          to={ticketPanelSearch(file.ticket_id, search)}
          className="shrink-0 text-xs text-muted-foreground hover:underline"
        >
          Ticket
        </Link>
      )}
    </li>
  );
}

/** Newest upload first; uploads in the same second keep the server's order. */
function newestFirst(files: ProjectFile[]): ProjectFile[] {
  return [...files].sort((a, b) => b.created_at - a.created_at);
}

/** A file's readable upload time, with the exact time on hover. */
export function FileUploadTime({ createdAt }: { createdAt: number }) {
  return (
    <time
      dateTime={new Date(createdAt * 1000).toISOString()}
      title={absoluteTime(createdAt * 1000)}
    >
      {formatUploadTime(createdAt)}
    </time>
  );
}
