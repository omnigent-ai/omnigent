import type { ReactNode } from "react";
import { AlertTriangleIcon, CopyIcon } from "lucide-react";
import {
  useArcaShutdownWarning,
  useMarkArcaWarningWhenVisible,
} from "@/hooks/useArcaShutdownWarning";
import { offersWorkweek } from "@/lib/arcaShutdownWarning";
import { cn } from "@/lib/utils";

function VisibleBanner({ now, children }: { now: Date; children: ReactNode }) {
  useMarkArcaWarningWhenVisible(now);
  return children;
}

export function ArcaShutdownBanner({
  hostId,
  hasTasks = false,
}: {
  hostId: string | null | undefined;
  hasTasks?: boolean;
}) {
  const warning = useArcaShutdownWarning();
  if (!warning.showForHost(hostId)) return null;

  const commands = ["arca extend overnight"];
  if (offersWorkweek(warning.now)) commands.push("arca extend workweek");

  return (
    <VisibleBanner now={warning.now}>
      <div
        role="status"
        className={cn(
          "arca-shutdown-banner shrink-0 px-3 md:px-4",
          hasTasks ? "mt-1" : "chat-plan-accordion mt-14 md:mt-12",
        )}
      >
        <div className="mx-auto flex max-w-3xl items-start gap-2 rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-foreground">
          <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden="true" />
          <div className="min-w-0 flex-1 text-ui">
            <p className="font-medium">
              Your Arca will shut down at about 6 PM unless you keep it running.
            </p>
            <div className="mt-1.5 flex flex-wrap gap-2">
              {commands.map((command) => (
                <div
                  key={command}
                  className="flex max-w-full items-center gap-1 rounded border border-border bg-background/70 px-2 py-0.5"
                >
                  <code className="min-w-0 break-all">{command}</code>
                  <button
                    type="button"
                    aria-label={`Copy ${command}`}
                    title={`Copy ${command}`}
                    className="rounded p-1 hover:bg-muted focus-visible:outline focus-visible:outline-2"
                    onClick={() => void navigator.clipboard.writeText(command)}
                  >
                    <CopyIcon className="size-3.5" aria-hidden="true" />
                  </button>
                </div>
              ))}
            </div>
            <p className="mt-1.5 text-muted-foreground">
              Run this on your laptop. Already extended? You can ignore this.
            </p>
            <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
              <button
                type="button"
                className="underline underline-offset-2 hover:no-underline"
                onClick={warning.dismissToday}
              >
                Not now
              </button>
              <button
                type="button"
                className="underline underline-offset-2 hover:no-underline"
                onClick={warning.optOut}
              >
                Don't remind me
              </button>
            </div>
          </div>
        </div>
      </div>
    </VisibleBanner>
  );
}
