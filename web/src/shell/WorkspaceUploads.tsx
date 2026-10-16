import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronUp, Upload } from "lucide-react";
import * as Progress from "radix-ui/progress";
import { toast } from "sonner";
import { useSession } from "@/hooks/useSession";
import { useViewerId } from "@/hooks/useViewerId";
import { useWorkspaceEnvironment } from "@/hooks/useWorkspaceChangedFiles";
import { useFileDropTarget } from "@/hooks/useFileDropTarget";
import { isEditorLevel } from "@/lib/permissionsApi";
import {
  getUploadCapability,
  UploadError,
  type UploadCapability,
  type UploadOwner,
} from "@/lib/workspaceUploadsApi";
import type { SessionStatus } from "@/lib/types";
import {
  cancelUpload,
  cancelUploads,
  enqueueUploads,
  retryUpload,
  retryDelay,
} from "@/lib/workspaceUploadController";
import {
  isActiveUpload,
  ownerKey,
  uploadTotals,
  useUploadStore,
  type UploadRow,
} from "@/store/uploadStore";
import { useChatStore } from "@/store/chatStore";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { formatBytes } from "./fileStatusUtils";

export function uploadGate(
  permission: number | null,
  capability: UploadCapability | null | undefined,
  status: SessionStatus | undefined,
  directory: string,
  archived = false,
) {
  const visible = permission !== null && isEditorLevel(permission) && capability != null;
  const reason = archived
    ? "Restore this archived session to upload files"
    : status === "failed"
      ? "Reconnect this session to upload files"
      : directory.startsWith("/") || directory.split("/").includes("..")
        ? "Uploads must stay inside the workspace"
        : capability && !capability.supported && capability.reason !== "runner_unavailable"
          ? capability.reason === "runner_capability_mismatch"
            ? "Update the host and restart the session to upload files"
            : (capability.reason ?? "Workspace uploads are unsupported")
          : undefined;
  return { visible, reason, enabled: visible && !reason };
}

interface UploadContextValue {
  owner: UploadOwner;
  capability: UploadCapability | null | undefined;
  visible: boolean;
  enabled: boolean;
  reason?: string;
  probeReason?: string;
  retryCapability?: () => void;
  probing: boolean;
  directory: string;
  destination: string;
  setDirectory: (directory: string) => void;
  /** Drop-root target: the mounted Files tree's location, else the workspace root. */
  dropDirectory: string;
  dropDestination: string;
  setTreeDirectory: (directory: string | null) => void;
  enqueue: (files: File[], directory?: string) => void;
}
const UploadContext = createContext<UploadContextValue | null>(null);

export function useWorkspaceUpload() {
  return useContext(UploadContext);
}

export function WorkspaceUploadProvider({
  sessionId,
  children,
}: {
  sessionId: string;
  children: ReactNode;
}) {
  const identity = useViewerId();
  const { session } = useSession(sessionId);
  const environment = useWorkspaceEnvironment(sessionId);
  const root = environment.data?.root ?? null;
  const [directories, setDirectories] = useState<Record<string, string>>({});
  const directory = directories[sessionId] ?? "";
  const setDirectory = useCallback(
    (next: string) =>
      setDirectories((current) =>
        current[sessionId] === next ? current : { ...current, [sessionId]: next },
      ),
    [sessionId],
  );
  // Only the Files tree browses; the Changes view mounts no tree, so its drops go to the root.
  const [treeDirectory, setTreeDirectory] = useState<string | null>(null);
  const client = useQueryClient();
  const liveStatus = useChatStore((state) =>
    state.conversationId === sessionId ? state.sessionStatus : undefined,
  );
  const permission = session?.permissionLevel ?? null;
  const owner = useMemo(
    () => ({
      origin: window.location.origin,
      identity: identity ?? "",
      session: sessionId,
      environment: "default",
    }),
    [identity, sessionId],
  );
  const canProbe = identity !== null && permission !== null && isEditorLevel(permission);
  const capability = useQuery({
    queryKey: ["workspace-upload-capability", owner.origin, owner.identity, sessionId],
    queryFn: () => getUploadCapability(owner),
    enabled: canProbe,
    staleTime: 30_000,
    retry: (attempt, error) =>
      (error instanceof TypeError ||
        (error instanceof UploadError && (error.status === 429 || error.status >= 500))) &&
      attempt < 5,
    retryDelay: (attempt, error) =>
      error instanceof UploadError && error.headers.has("Retry-After")
        ? retryDelay(error, attempt)
        : Math.min(30_000, retryDelay(error, attempt)),
  });
  const { refetch: refetchCapability } = capability;
  const probeError = capability.failureReason ?? capability.error;
  const evidence = useRef({ owner: "", answered: false });
  const group = ownerKey(owner);
  if (evidence.current.owner !== group) evidence.current = { owner: group, answered: false };
  if (capability.data === null) evidence.current.answered = false;
  const responseProvesRoute =
    capability.data != null ||
    (probeError instanceof UploadError &&
      [
        "unauthorized",
        "forbidden",
        "permission_denied",
        "busy",
        "upload_limit",
        "runner_unavailable",
        "runner_capability_mismatch",
        "upload_relay_failed",
        "upload_timeout",
      ].includes(probeError.code));
  if (responseProvesRoute) evidence.current.answered = true;
  const routeAnswered = evidence.current.answered;
  const probeReason = !routeAnswered
    ? undefined
    : probeError instanceof UploadError
      ? probeError.status === 401
        ? "Sign in again to upload files, then retry"
        : probeError.status === 403
          ? "Upload permission denied. Ask the session owner for access, then retry"
          : probeError.status === 429
            ? "Upload service is busy. Retry availability shortly"
            : probeError.status === 503
              ? "Upload service is unavailable. Retry availability"
              : "Could not check upload availability. Retry the check"
      : probeError
        ? "Could not check upload availability. Retry the check"
        : undefined;
  const gate = useMemo(() => {
    const current = uploadGate(
      permission,
      capability.data,
      liveStatus ?? session?.status,
      directory,
      session?.archived,
    );
    if (!probeReason || permission === null || !isEditorLevel(permission)) return current;
    return {
      visible: current.visible || routeAnswered,
      enabled: false,
      reason: probeReason,
    };
  }, [
    permission,
    capability.data,
    liveStatus,
    session?.status,
    session?.archived,
    directory,
    probeReason,
    routeAnswered,
  ]);
  const rootName = root?.split("/").filter(Boolean).at(-1) || "workspace";
  const destination = directory || rootName;
  const value = useMemo<UploadContextValue>(
    () => ({
      owner,
      capability: capability.data,
      ...gate,
      probing: capability.isFetching,
      probeReason: canProbe ? probeReason : undefined,
      retryCapability:
        canProbe && probeReason
          ? () => {
              void refetchCapability();
            }
          : undefined,
      directory,
      destination,
      setDirectory,
      dropDirectory: treeDirectory ?? "",
      dropDestination: treeDirectory || rootName,
      setTreeDirectory,
      enqueue: (files, target = directory) => {
        if (!gate.enabled || !capability.data) {
          toast.error(gate.reason ?? "Workspace upload permission is pending");
          return;
        }
        enqueueUploads(owner, files, target, target || rootName, capability.data.limits, client);
      },
    }),
    [
      owner,
      capability.data,
      capability.isFetching,
      refetchCapability,
      canProbe,
      probeReason,
      gate,
      directory,
      destination,
      setDirectory,
      treeDirectory,
      rootName,
      client,
    ],
  );
  return <UploadContext.Provider value={value}>{children}</UploadContext.Provider>;
}

export function UploadPicker({ directory }: { directory: string }) {
  const context = useContext(UploadContext);
  const errorId = useId();
  const input = useRef<HTMLInputElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const selectedDirectory = useRef(directory);
  const setDirectory = context?.setDirectory;
  useEffect(() => setDirectory?.(directory), [setDirectory, directory]);
  useEffect(() => {
    const element = input.current;
    const restoreFocus = () => button.current?.focus();
    element?.addEventListener("cancel", restoreFocus);
    return () => element?.removeEventListener("cancel", restoreFocus);
  }, [context?.visible]);
  if (!context || (!context.visible && !context.retryCapability)) return null;
  return (
    <>
      {context.visible && (
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <span title={context.reason}>
                <Button
                  ref={button}
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Upload files"
                  disabled={!context.enabled}
                  onClick={() => {
                    selectedDirectory.current = directory;
                    input.current?.click();
                  }}
                >
                  <Upload />
                </Button>
              </span>
            </TooltipTrigger>
            <TooltipContent>{context.reason ?? "Upload files to this folder"}</TooltipContent>
          </Tooltip>
        </TooltipProvider>
      )}
      {context.probeReason && (
        <p id={errorId} role="alert" className="max-w-48 text-destructive">
          {context.probeReason}
        </p>
      )}
      {context.retryCapability && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-label="Retry upload availability"
          aria-describedby={errorId}
          disabled={context.probing}
          onClick={context.retryCapability}
        >
          Retry
        </Button>
      )}
      {context.visible && (
        <input
          ref={input}
          type="file"
          multiple
          className="hidden"
          aria-label="Choose workspace files"
          onChange={(event) => {
            context.enqueue(Array.from(event.target.files ?? []), selectedDirectory.current);
            event.target.value = "";
            button.current?.focus();
          }}
        />
      )}
    </>
  );
}

export function resolveUploadTarget(event: DragEvent, browsedDirectory: string): string {
  const row =
    event.target instanceof Element ? event.target.closest<HTMLElement>("[data-drop-dir]") : null;
  return [
    browsedDirectory.replace(/\/$/, ""),
    row?.closest("[data-upload-tree]") ? row.dataset.dropDir : undefined,
  ]
    .filter(Boolean)
    .join("/");
}

export function droppedUploadFiles(event: DragEvent, files: File[]): File[] {
  const directories = Array.from(event.dataTransfer?.items ?? []).filter(
    (item) => item.webkitGetAsEntry?.()?.isDirectory,
  );
  if (!directories.length) return files;
  toast.error("Folders cannot be uploaded. Select their files instead.");
  const directoryFiles = new Set(directories.map((item) => item.getAsFile()).filter(Boolean));
  return files.filter((file) => !directoryFiles.has(file));
}

export function WorkspaceUploadRootIndicator({
  container,
  destination,
}: {
  container: HTMLElement;
  destination: string;
}) {
  return createPortal(
    <div
      aria-hidden="true"
      data-drop-variant="upload"
      className="pointer-events-none absolute inset-0 z-40 m-2 rounded-md border-2 border-dashed border-info bg-transparent"
    >
      <div
        data-upload-root-label=""
        className="absolute top-0 left-1/2 flex max-w-[calc(100%-1rem)] -translate-x-1/2 flex-col items-center rounded-b-md border-x border-b border-info bg-card-solid px-2 py-0.5 text-center text-ui text-foreground shadow-composer"
      >
        <span className="whitespace-nowrap font-medium">Upload to workspace</span>
        <span className="max-w-full truncate text-muted-foreground">to {destination}</span>
      </div>
    </div>,
    container,
  );
}

export function WorkspaceUploadDropRoot({ children }: { children: ReactNode }) {
  const context = useContext(UploadContext);
  const [target, setTarget] = useState<HTMLDivElement | null>(null);
  const active = useFileDropTarget(
    context?.enabled ? target : null,
    (files, event) => {
      if (context)
        context.enqueue(
          droppedUploadFiles(event, files),
          resolveUploadTarget(event, context.dropDirectory),
        );
    },
    {
      ignore: (event) =>
        event.target instanceof Element && event.target.closest("[data-upload-tree]") !== null,
    },
  );
  return (
    <div
      ref={setTarget}
      data-upload-drop-root={context?.visible ? "" : undefined}
      data-upload-disabled-reason={
        context?.visible && !context.enabled ? context.reason : undefined
      }
      className="relative flex min-h-0 flex-1 flex-col overflow-hidden"
    >
      {children}
      <span className="sr-only" role="status" aria-live="polite">
        {active && context ? `Upload to ${context.dropDestination}` : ""}
      </span>
      {active && target && context && (
        <WorkspaceUploadRootIndicator container={target} destination={context.dropDestination} />
      )}
    </div>
  );
}

function uploadPercent(row: UploadRow): number {
  return row.size ? (row.offset / row.size) * 100 : row.status === "done" ? 100 : 0;
}

export function UploadsTray({ visible }: { visible: boolean }) {
  const context = useContext(UploadContext);
  const allRows = useUploadStore((state) => state.rows);
  const client = useQueryClient();
  const [expanded, setExpanded] = useState(true);
  const group = context ? ownerKey(context.owner) : "";
  const rows = useMemo(
    () => Object.values(allRows).filter((row) => ownerKey(row) === group),
    [allRows, group],
  );
  const totals = uploadTotals(rows);
  const summary = `${totals.done}/${rows.length} files · ${formatBytes(totals.committed)} / ${formatBytes(totals.total)}`;
  useEffect(() => {
    if (visible && context?.visible) return useUploadStore.getState().showTray(group);
    return undefined;
  }, [group, visible, context?.visible]);
  if (!context?.visible || !rows.length) return null;
  return (
    <section role="region" aria-label="Uploads" className="shrink-0 border-t border-border text-ui">
      <div className="flex items-center gap-2 px-2 py-1.5">
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          <Upload className="size-4 shrink-0" />
          <span className="truncate tabular-nums" title={summary}>
            {summary}
          </span>
          {expanded ? <ChevronDown className="size-3" /> : <ChevronUp className="size-3" />}
        </button>
        {!totals.active && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => useUploadStore.getState().dismiss(rows.map((row) => row.key))}
          >
            Dismiss
          </Button>
        )}
      </div>
      {expanded && (
        <>
          <div className="max-h-56 overflow-auto px-2">
            {rows.map((row) => (
              <div
                key={row.key}
                className="space-y-1 border-t border-border py-2"
                data-upload-state={row.status}
              >
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate font-medium" title={row.name}>
                    {row.name}
                  </span>
                  {isActiveUpload(row) && (
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Cancel ${row.name}`}
                      disabled={row.cancellationPending}
                      onClick={() => void cancelUpload(row.key)}
                    >
                      Cancel
                    </Button>
                  )}
                  {row.status === "failed" && (
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Retry ${row.name}`}
                      disabled={row.cancellationPending}
                      onClick={() => void retryUpload(row.key, client)}
                    >
                      Retry
                    </Button>
                  )}
                </div>
                <p className="truncate text-muted-foreground" title={row.destination}>
                  Uploading to {row.destination}
                </p>
                <Progress.Root
                  aria-label={`${row.name} upload progress`}
                  value={uploadPercent(row)}
                  className="relative flex h-1 w-full overflow-hidden rounded-full bg-muted"
                >
                  <Progress.Indicator
                    className="h-full bg-primary"
                    style={{
                      width: `${uploadPercent(row)}%`,
                    }}
                  />
                </Progress.Root>
                <div className="flex justify-between gap-2 text-muted-foreground">
                  <span>
                    {row.status === "connecting"
                      ? "Connecting…"
                      : row.status.charAt(0).toUpperCase() + row.status.slice(1)}
                  </span>
                  <span className="tabular-nums">
                    {formatBytes(row.offset)} / {formatBytes(row.size)}
                  </span>
                </div>
                {row.error && (
                  <p
                    role={
                      row.status === "failed" || row.status === "cancelled" ? "alert" : undefined
                    }
                    className="text-destructive"
                  >
                    {row.error}
                  </p>
                )}
              </div>
            ))}
          </div>
          <div className="flex flex-wrap gap-1 px-2 py-1">
            {totals.active && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() =>
                  void cancelUploads(rows.filter(isActiveUpload).map((row) => row.key))
                }
              >
                Cancel remaining
              </Button>
            )}
            {rows.some((row) => row.status === "failed" && !row.cancellationPending) && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() =>
                  rows
                    .filter((row) => row.status === "failed" && !row.cancellationPending)
                    .forEach((row) => void retryUpload(row.key, client))
                }
              >
                Retry failed
              </Button>
            )}
          </div>
        </>
      )}
    </section>
  );
}
