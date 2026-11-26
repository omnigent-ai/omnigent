import { useState } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { FolderOpenIcon, TicketIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Spinner } from "@/components/ui/spinner";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import {
  getTicket,
  listProjectFiles,
  openProjectFile,
  type ProjectFile,
} from "@/lib/projectTicketsApi";
import { absoluteTime, formatUploadTime } from "@/lib/relativeTime";
import { Link, useSearchParams } from "@/lib/routing";
import { formatBytes } from "@/shell/fileStatusUtils";
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
  const ticketTitles = useLinkedTicketTitles(projectId, open ? files.data : undefined);
  if (projectId === null) return null;
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="ghost" size={isMobile ? "icon" : "sm"} aria-label="Files">
          <FolderOpenIcon />
          {!isMobile && <span className="@max-[60rem]/topbar:hidden">Files</span>}
        </Button>
      </PopoverTrigger>
      {/* Opaque so the board behind doesn't compete with filenames. */}
      <PopoverContent
        align="end"
        collisionPadding={12}
        className="menu-surface w-[min(26rem,calc(100vw-24px))] gap-0 bg-card-solid! p-0"
      >
        <div className="flex items-baseline gap-2 border-b border-border/60 px-4 py-3">
          <span className="text-sm font-semibold">Project files</span>
          {files.data && files.data.length > 0 && (
            <span className="text-xs text-muted-foreground tabular-nums">{files.data.length}</span>
          )}
        </div>
        {files.isLoading ? (
          <div className="flex justify-center p-4">
            <Spinner />
          </div>
        ) : files.error && !files.data ? (
          <TicketLoadError error={files.error} className="m-3" />
        ) : files.data?.length ? (
          <ul className="max-h-[min(28rem,70vh)] divide-y divide-border/60 overflow-y-auto">
            {newestFirst(files.data).map((file) => (
              <ProjectFileRow
                key={file.id}
                file={file}
                ticketTitle={file.ticket_id ? ticketTitles.get(file.ticket_id) : undefined}
              />
            ))}
          </ul>
        ) : (
          <p className="px-4 py-3 text-sm text-muted-foreground">
            No files yet. Plans, reports, screenshots and other files your team attaches to tickets
            show up here.
          </p>
        )}
      </PopoverContent>
    </Popover>
  );
}

/** At most this many linked tickets are looked up per open list. */
const MAX_TICKET_LOOKUPS = 25;

/** Titles of the tickets files belong to, so rows can name them; unknown ones stay generic. */
function useLinkedTicketTitles(
  projectId: string | null,
  files: ProjectFile[] | undefined,
): Map<string, string> {
  const ticketIds = [
    ...new Set((files ?? []).flatMap((file) => (file.ticket_id ? [file.ticket_id] : []))),
  ].slice(0, MAX_TICKET_LOOKUPS);
  const tickets = useQueries({
    queries: ticketIds.map((ticketId) => ({
      queryKey: ["project-tickets", projectId, ticketId],
      queryFn: () => getTicket(projectId!, ticketId),
      enabled: projectId !== null,
      staleTime: 60_000,
      retry: false,
    })),
  });
  const titles = new Map<string, string>();
  for (const ticket of tickets) if (ticket.data) titles.set(ticket.data.id, ticket.data.title);
  return titles;
}

/** A short format name from the filename's extension, such as "MD" or "PNG". */
function fileFormatLabel(filename: string): string {
  const name = filename.split("/").at(-1) ?? filename;
  const dot = name.lastIndexOf(".");
  return dot > 0 && dot < name.length - 1 ? name.slice(dot + 1).toUpperCase() : "File";
}

function ProjectFileRow({ file, ticketTitle }: { file: ProjectFile; ticketTitle?: string }) {
  const [search] = useSearchParams();
  const [error, setError] = useState<string | null>(null);
  return (
    <li className="flex items-start gap-3 px-4 py-3">
      <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md bg-muted">
        <WorkspaceFileIcon path={file.filename} mimeType={file.content_type} className="size-4" />
      </span>
      <div className="min-w-0 flex-1">
        <button
          type="button"
          className="block w-full min-w-0 text-left"
          onClick={() => {
            setError(null);
            openProjectFile(file.url, file.filename).catch((reason: unknown) =>
              setError(reason instanceof Error ? reason.message : "Couldn't open the file"),
            );
          }}
        >
          <span className="block truncate text-sm font-semibold text-foreground hover:underline">
            {file.title}
          </span>
          <span
            className={
              error
                ? "block text-xs text-destructive"
                : "block truncate text-xs text-muted-foreground"
            }
          >
            {error ?? (
              <>
                {fileFormatLabel(file.filename)} · {formatBytes(file.bytes)} ·{" "}
                <FileUploadTime createdAt={file.created_at} />
              </>
            )}
          </span>
        </button>
        {file.ticket_id && (
          <Link
            to={ticketPanelSearch(file.ticket_id, search)}
            title={ticketTitle ? `Open ticket: ${ticketTitle}` : "Open ticket"}
            className="mt-1 flex min-w-0 items-center gap-1 text-xs text-muted-foreground hover:text-foreground hover:underline"
          >
            <TicketIcon aria-hidden className="size-3 shrink-0" />
            <span className="truncate">{ticketTitle ?? "Ticket"}</span>
          </Link>
        )}
      </div>
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
