import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { FileTextIcon, FolderOpenIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Spinner } from "@/components/ui/spinner";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { listProjectFiles, openProjectFile, type ProjectFile } from "@/lib/projectTicketsApi";
import { relativeTime } from "@/lib/relativeTime";
import { Link, useSearchParams } from "@/lib/routing";
import { ticketPanelSearch } from "./projectHomeNavigation";

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
          {!isMobile && <span className="max-xl:hidden">Files</span>}
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
        ) : files.error ? (
          <p className="p-3 text-sm text-destructive">{files.error.message}</p>
        ) : files.data?.length ? (
          <ul className="max-h-96 divide-y divide-border/60 overflow-y-auto">
            {files.data.map((file) => (
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
      <FileTextIcon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
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
          {error ?? relativeTime(file.created_at * 1000)}
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
