import { Loader2Icon, PauseIcon, WifiOffIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { CHAT_COLUMN_WIDTH } from "@/pages/chatLayout";

/** A session on a static sandbox whose host is offline (see `StaticSandboxWakeBanner`). */
export interface PausedSandbox {
  /** The sandbox id, e.g. "foo". */
  name: string;
  state: "running" | "paused" | "gone";
  /** True while a wake request is in flight. */
  waking: boolean;
  onResume: () => void;
  onRemove: () => void;
}

/**
 * The quiet "⏸ foo is paused" chip for a session whose static sandbox VM paused, for
 * people only viewing files or the terminal. Resume wakes it in place; a gone
 * VM offers removing its entry instead, never Resume.
 */
export function StaticSandboxWakeBanner({ sandbox }: { sandbox: PausedSandbox }) {
  const gone = sandbox.state === "gone";
  return (
    <div
      className={cn(
        "mx-auto mb-4 flex w-full items-center justify-center gap-2 px-6 py-1.5 text-sm text-muted-foreground",
        CHAT_COLUMN_WIDTH,
      )}
      data-testid="paused-sandbox-chip"
      role="status"
    >
      {gone ? (
        <>
          <WifiOffIcon className="size-3.5 shrink-0 text-destructive" aria-hidden />
          <span>VM {sandbox.name} no longer exists</span>
          <Button
            variant="outline"
            size="sm"
            className="h-6 px-2 text-xs"
            onClick={sandbox.onRemove}
            data-testid="paused-sandbox-remove"
          >
            Remove entry
          </Button>
        </>
      ) : sandbox.waking ? (
        <>
          <Loader2Icon className="size-3.5 shrink-0 animate-spin" aria-hidden />
          <span>Waking {sandbox.name}…</span>
        </>
      ) : (
        <>
          <PauseIcon className="size-3.5 shrink-0" aria-hidden />
          <span>{sandbox.name} is paused</span>
          <Button
            variant="outline"
            size="sm"
            className="h-6 px-2 text-xs"
            onClick={sandbox.onResume}
            data-testid="paused-sandbox-resume"
          >
            Resume
          </Button>
        </>
      )}
    </div>
  );
}
