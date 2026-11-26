import { AlertCircleIcon, CloudOffIcon } from "lucide-react";

import { SandboxResumePulse } from "@/components/SandboxResumePulse";
import { Button } from "@/components/ui/button";
import { type SandboxResumeView, useSandboxResume } from "@/hooks/useSandboxResume";
import { cn } from "@/lib/utils";
import { RESUME_SANDBOX_LABEL } from "@/shell/resumeSandboxAvailability";

/**
 * Where the notice sits: centered in an empty panel, at the top of a list, or
 * as a one-line strip above content cached from before the sandbox stopped.
 */
export type SandboxResumeNoticeLayout = "panel" | "inline" | "banner";

/**
 * The one way every workspace surface shows a stopped sandbox: asleep with
 * Resume, resuming, or failed with Retry. Its parts come back on their own
 * once the sandbox is online, so the notice offers nothing else to do.
 */
export function SandboxResumeNotice({
  sessionId,
  layout = "panel",
  /** What the sandbox needs to be up for, as in "Resume it to use the terminal." */
  purpose,
  className,
}: {
  sessionId: string | null | undefined;
  layout?: SandboxResumeNoticeLayout;
  purpose?: string;
  className?: string;
}) {
  const view = useSandboxResume(sessionId);
  return (
    <SandboxResumeNoticeView view={view} layout={layout} purpose={purpose} className={className} />
  );
}

/** What the notice shows: the sandbox's status, why it failed, and whether this viewer may resume. */
export type SandboxResumeNoticeState = Pick<
  SandboxResumeView,
  "status" | "failure" | "availability" | "resume"
>;

/**
 * The notice for a status a caller already resolved, as a terminal tab does when its own waits
 * have run out and it shows the sandbox asleep or failed instead of the reported progress.
 */
export function SandboxResumeNoticeView({
  view: { status, failure, availability, resume },
  layout = "panel",
  purpose,
  className,
}: {
  view: SandboxResumeNoticeState;
  layout?: SandboxResumeNoticeLayout;
  purpose?: string;
  className?: string;
}) {
  if (status === "none") return null;
  const deniedReason = availability.available ? null : (availability.reason ?? null);
  const canResume = availability.available;

  let title: string;
  let detail: string;
  if (status === "resuming") {
    title = "Resuming sandbox…";
    detail = "This usually takes under a minute. Everything reconnects on its own.";
  } else if (status === "failed") {
    // The failure says what went wrong; the title says where that leaves the sandbox.
    title = "Sandbox is asleep";
    detail = failure ?? "Couldn't resume the sandbox. Try again.";
  } else {
    title = "Sandbox is asleep";
    detail =
      deniedReason ??
      (purpose ? `Resume it to ${purpose}.` : "Resume it to pick up where you left off.");
  }
  const action =
    status === "resuming" || !canResume ? null : (
      <Button
        type="button"
        size="sm"
        variant={status === "failed" || layout !== "panel" ? "secondary" : "default"}
        onClick={resume}
        componentId="sandbox.resume"
        data-testid="sandbox-resume-button"
      >
        {status === "failed" ? "Retry" : RESUME_SANDBOX_LABEL}
      </Button>
    );
  const icon =
    status === "resuming" ? (
      <SandboxResumePulse />
    ) : status === "failed" ? (
      <AlertCircleIcon className="size-4 shrink-0 text-destructive" aria-hidden />
    ) : (
      <CloudOffIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
    );
  // A failure is announced once; progress politely.
  const live =
    status === "failed"
      ? ({ role: "alert" } as const)
      : ({ role: "status", "aria-live": "polite" } as const);

  if (layout === "banner") {
    return (
      <div
        {...live}
        data-testid="sandbox-resume-notice"
        data-status={status}
        className={cn(
          "flex shrink-0 flex-wrap items-center justify-center gap-x-3 gap-y-1.5 border-b border-border bg-muted/40 px-3 py-2 text-sm",
          className,
        )}
      >
        <span className="flex min-w-0 items-center gap-2">
          {icon}
          <span className="whitespace-nowrap font-medium text-foreground">{title}</span>
          {/* One calm line while resuming; the strip only explains a failure or a denial. */}
          {status === "failed" || deniedReason ? (
            <span
              className={cn(
                "min-w-0 [overflow-wrap:anywhere]",
                status === "failed" ? "text-destructive" : "text-muted-foreground",
              )}
            >
              {detail}
            </span>
          ) : null}
        </span>
        {action}
      </div>
    );
  }

  const centered = layout === "panel";
  return (
    <div
      {...live}
      data-testid="sandbox-resume-notice"
      data-status={status}
      className={cn(
        "flex flex-col gap-3",
        centered
          ? "flex-1 items-center justify-center px-6 py-8 text-center"
          : "items-start px-2 py-1.5",
        className,
      )}
    >
      <div className={cn("space-y-1", centered ? "max-w-sm" : "")}>
        <p
          className={cn(
            "flex items-center gap-2 font-medium text-foreground",
            centered ? "justify-center text-ui" : "text-sm",
          )}
        >
          {icon}
          {title}
        </p>
        <p
          className={cn(
            "text-sm [overflow-wrap:anywhere]",
            status === "failed" ? "text-destructive" : "text-muted-foreground",
          )}
        >
          {detail}
        </p>
      </div>
      {action}
    </div>
  );
}
