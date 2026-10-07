import { useEffect, useRef, useState } from "react";
import { AlertTriangleIcon, CheckIcon, CopyIcon } from "lucide-react";
import { useMarkArcaBannerWhenVisible } from "@/hooks/useArcaShutdownBanner";
import { offersWorkweek } from "@/lib/arcaShutdownWarning";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/utils";

function CommandCopyButton({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  const resetTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (resetTimeout.current) clearTimeout(resetTimeout.current);
    };
  }, []);

  const copy = async () => {
    try {
      await copyText(command);
      setCopied(true);
      if (resetTimeout.current) clearTimeout(resetTimeout.current);
      resetTimeout.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="flex max-w-full items-center gap-1 rounded border border-border bg-background/70 px-2 py-0.5">
      <code className="min-w-0 break-all">{command}</code>
      <button
        type="button"
        aria-label={copied ? `Copied ${command}` : `Copy ${command}`}
        title={copied ? "Copied" : `Copy ${command}`}
        className="rounded p-1 hover:bg-muted focus-visible:outline focus-visible:outline-2"
        onClick={() => void copy()}
      >
        {copied ? (
          <CheckIcon className="size-3.5" aria-hidden="true" />
        ) : (
          <CopyIcon className="size-3.5" aria-hidden="true" />
        )}
      </button>
    </div>
  );
}

export function ArcaShutdownBanner({
  warning,
  hasTasks = false,
}: {
  warning: { day: string; dismissToday: () => void; optOut: () => void };
  hasTasks?: boolean;
}) {
  useMarkArcaBannerWhenVisible(warning.day);
  const commands = ["arca extend overnight"];
  if (offersWorkweek(new Date(`${warning.day}T12:00:00`))) commands.push("arca extend workweek");

  return (
    <div
      role="status"
      className={cn(
        "shrink-0 px-3 md:px-4",
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
              <CommandCopyButton key={command} command={command} />
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
  );
}
