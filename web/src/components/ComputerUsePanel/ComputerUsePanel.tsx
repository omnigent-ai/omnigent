import {
  ActivityIcon,
  ArrowUpDownIcon,
  CheckCircle2Icon,
  CircleStopIcon,
  EyeIcon,
  HandIcon,
  ImageOffIcon,
  KeyboardIcon,
  MonitorIcon,
  MousePointerClickIcon,
  MoveIcon,
  OctagonXIcon,
  PauseCircleIcon,
  TextCursorInputIcon,
  TextSelectIcon,
} from "lucide-react";
import { SessionImage } from "@/components/SessionImage";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import type {
  ComputerUseActionKind,
  ComputerUseStatus,
  ComputerUseViewModel,
} from "@/lib/computerUse";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";

export interface ComputerUsePanelProps {
  conversationId: string;
  viewModel: ComputerUseViewModel | null;
  className?: string;
}

const STATUS_LABEL: Record<ComputerUseStatus, string> = {
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  interrupted: "Interrupted",
};

function StatusIcon({ status }: { status: ComputerUseStatus }) {
  if (status === "running") return <MonitorIcon className="size-4 animate-pulse" />;
  if (status === "completed") return <CheckCircle2Icon className="size-4" />;
  if (status === "failed") return <OctagonXIcon className="size-4" />;
  return <PauseCircleIcon className="size-4" />;
}

const ACTION_DETAILS = {
  inspect: { label: "Inspecting", Icon: EyeIcon },
  click: { label: "Clicking", Icon: MousePointerClickIcon },
  scroll: { label: "Scrolling", Icon: ArrowUpDownIcon },
  type: { label: "Typing", Icon: TextCursorInputIcon },
  select: { label: "Selecting", Icon: TextSelectIcon },
  drag: { label: "Dragging", Icon: MoveIcon },
  key: { label: "Pressing keys", Icon: KeyboardIcon },
  interact: { label: "Interacting", Icon: HandIcon },
} satisfies Record<ComputerUseActionKind, { label: string; Icon: typeof EyeIcon }>;

function ComputerActions({ actionKinds }: { actionKinds?: ComputerUseActionKind[] }) {
  if (!actionKinds?.length) {
    return (
      <ul aria-label="Computer actions" className="mt-3 flex flex-wrap gap-1.5">
        <li className="flex items-center gap-1.5 rounded-full border border-border bg-muted/50 px-2 py-1 text-xs text-muted-foreground">
          <ActivityIcon aria-hidden="true" className="size-3.5" />
          Using computer
        </li>
      </ul>
    );
  }

  return (
    <ul aria-label="Computer actions" className="mt-3 flex flex-wrap gap-1.5">
      {actionKinds.map((actionKind) => {
        const { label, Icon } = ACTION_DETAILS[actionKind];
        return (
          <li
            key={actionKind}
            className="flex items-center gap-1.5 rounded-full border border-border bg-muted/50 px-2 py-1 text-xs text-muted-foreground"
          >
            <Icon aria-hidden="true" className="size-3.5" />
            {label}
          </li>
        );
      })}
    </ul>
  );
}

/** Provider-neutral latest-frame preview for native harness computer use. */
export function ComputerUsePanel({ conversationId, viewModel, className }: ComputerUsePanelProps) {
  if (viewModel === null) {
    return (
      <div
        role="status"
        className={cn(
          "flex min-h-0 flex-1 flex-col items-center justify-center gap-2 p-6 text-center text-muted-foreground",
          className,
        )}
      >
        <ImageOffIcon className="size-6" />
        <p className="text-ui font-medium text-foreground">Computer Use unavailable</p>
        <p className="max-w-64 text-sm">No classified computer activity is available.</p>
      </div>
    );
  }

  const { presentation, status, frame, error } = viewModel;
  const provider = presentation.provider === "codex" ? "Codex" : "Claude";
  const app = presentation.appName ?? presentation.appId ?? "Unknown app";
  const action = presentation.actionLabel ?? "Using computer";
  const statusLabel = STATUS_LABEL[status];
  const framePath = frame
    ? `/v1/sessions/${encodeURIComponent(conversationId)}/resources/files/${encodeURIComponent(frame.fileId)}/content`
    : undefined;

  return (
    <section
      aria-label="Computer Use"
      className={cn("flex min-h-0 flex-1 flex-col overflow-y-auto p-3", className)}
    >
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-2">
            <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-xs font-medium">
              {provider}
            </span>
            <h2 className="truncate text-ui font-semibold" title={app}>
              {app}
            </h2>
          </div>
          <p className="mt-1 line-clamp-2 text-sm text-muted-foreground" title={action}>
            {action}
          </p>
        </div>
        <div
          role="status"
          aria-live="polite"
          aria-label={`${provider} computer use ${statusLabel.toLowerCase()}: ${app}. ${action}`}
          className={cn(
            "flex shrink-0 items-center gap-1.5 rounded-full px-2 py-1 text-xs font-medium",
            status === "running" && "bg-primary/10 text-primary",
            status === "completed" && "bg-success/10 text-success",
            status === "failed" && "bg-destructive/10 text-destructive",
            status === "interrupted" && "bg-muted text-muted-foreground",
          )}
        >
          <StatusIcon status={status} />
          {statusLabel}
        </div>
      </div>

      <ComputerActions actionKinds={presentation.actionKinds} />

      <div className="mt-2 min-w-0 overflow-hidden rounded-lg border border-border bg-muted/30">
        {frame ? (
          <SessionImage
            path={framePath}
            alt={`Latest ${app} frame`}
            className="w-full rounded-none object-contain"
            // The frame is why the panel is open, so it must not wait on a
            // lazy trigger the panel's layout may never produce.
            eager
          />
        ) : status === "running" ? (
          <div
            role="status"
            aria-label="Loading computer preview"
            className="flex h-64 min-w-0 items-center justify-center text-muted-foreground"
          >
            <Spinner />
          </div>
        ) : (
          <div
            role="img"
            aria-label="No computer preview available"
            className="flex h-64 min-w-0 flex-col items-center justify-center gap-2 px-4 text-center text-muted-foreground"
          >
            <ImageOffIcon className="size-6" />
            <p className="text-sm">No preview is available for this action.</p>
          </div>
        )}
      </div>

      {error && (
        <p role="alert" className="mt-3 break-words text-sm text-destructive">
          {error}
        </p>
      )}

      {status === "running" && (
        <div className="mt-auto pt-3">
          <Button
            type="button"
            variant="outline"
            className="w-full gap-2"
            onClick={() => useChatStore.getState().stop()}
          >
            <CircleStopIcon className="size-4" />
            Stop
          </Button>
        </div>
      )}
    </section>
  );
}
