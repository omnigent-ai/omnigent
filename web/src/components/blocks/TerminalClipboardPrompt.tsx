import { ClipboardCopyIcon, XIcon } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";

export type TerminalClipboardDecision = "allow" | "once" | "block";

interface TerminalClipboardPromptProps {
  needsClick: boolean;
  copyFailed: boolean;
  onDecision: (decision: TerminalClipboardDecision, remember: boolean) => void;
  onRetry: () => void;
  onDismiss: () => void;
}

export function TerminalClipboardPrompt(props: TerminalClipboardPromptProps) {
  const toastId = useId();
  const { needsClick, copyFailed } = props;
  const latestProps = useRef(props);
  const mounted = useRef(true);

  useLayoutEffect(() => {
    latestProps.current = props;
  });

  useEffect(() => {
    mounted.current = true;
    return () => {
      // Sonner retains the card during its exit animation.
      mounted.current = false;
      toast.dismiss(toastId);
    };
  }, [toastId]);

  useEffect(() => {
    toast.custom(
      () => (
        <TerminalClipboardPromptContent
          needsClick={needsClick}
          copyFailed={copyFailed}
          onDecision={(decision, remember) => {
            if (mounted.current) latestProps.current.onDecision(decision, remember);
          }}
          onRetry={() => {
            if (mounted.current) latestProps.current.onRetry();
          }}
          onDismiss={() => {
            if (mounted.current) latestProps.current.onDismiss();
          }}
        />
      ),
      {
        // Inherit the toaster position so ordinary notices share the same stack measurements.
        id: toastId,
        duration: Number.POSITIVE_INFINITY,
        dismissible: false,
        style: {
          width: "min(30rem, calc(100vw - 2rem))",
          bottom: "max(0px, 100lvh - var(--omnigent-viewport-height, 100lvh))",
          touchAction: "auto",
        },
      },
    );
  }, [toastId, needsClick, copyFailed]);

  return null;
}

function TerminalClipboardPromptContent({
  needsClick,
  copyFailed,
  onDecision,
  onRetry,
  onDismiss,
}: TerminalClipboardPromptProps) {
  const [remember, setRemember] = useState(true);
  const titleId = useId();
  const descriptionId = useId();

  return (
    <section
      role="region"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      data-testid="terminal-clipboard-consent"
      className="relative flex w-full flex-col overflow-hidden rounded-xl border-2 border-primary/50 bg-card-solid p-5 text-left text-ui text-card-foreground shadow-2xl"
      style={{
        maxHeight:
          "calc(var(--omnigent-viewport-height, 100dvh) - var(--offset-bottom, 1rem) - var(--omnigent-inset-top, 0px) - var(--offset, 0px) - 1rem)",
      }}
    >
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        className="absolute top-3 right-3"
        aria-label="Dismiss clipboard request"
        onClick={onDismiss}
      >
        <XIcon className="size-4" />
      </Button>
      <div className="min-h-0 overflow-y-auto overscroll-contain">
        <div role="status">
          <div className="flex items-start gap-3 pr-6">
            <div className="rounded-lg bg-primary/10 p-2.5 text-primary" aria-hidden="true">
              <ClipboardCopyIcon className="size-6" />
            </div>
            <h3 id={titleId} className="min-w-0 text-lg leading-snug font-semibold">
              {needsClick ? "Copy needs a click" : "Allow this terminal to copy to your clipboard?"}
            </h3>
          </div>
          <p id={descriptionId} className="mt-3 text-ui leading-relaxed text-muted-foreground">
            {needsClick
              ? copyFailed
                ? "Your selection hasn’t been copied. Check your browser’s clipboard permissions and try again."
                : "Your browser needs a click to finish copying. Your saved clipboard preference hasn’t changed."
              : "Your selection hasn’t been copied yet. Your permission is required because terminal programs can silently replace your clipboard with text or commands you didn’t intend to paste."}
          </p>
        </div>
        {!needsClick && (
          <div className="mt-5">
            <label className="flex cursor-pointer items-start gap-2">
              <input
                type="checkbox"
                checked={remember}
                onChange={(event) => setRemember(event.target.checked)}
                className="mt-0.5 size-4 shrink-0 accent-primary"
              />
              <span>Remember my choice</span>
            </label>
            <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
              {remember
                ? "Change this in Settings → General."
                : "Allow or Block will apply only while this terminal stays open."}
            </p>
          </div>
        )}
      </div>
      <div className="mt-5 flex shrink-0 flex-wrap gap-2 border-t border-border pt-4 [&>button]:h-10 [&>button]:px-4">
        {needsClick ? (
          <>
            <Button type="button" onClick={onRetry} componentId="diagnostics.terminal.copy">
              Copy now
            </Button>
            <Button type="button" variant="secondary" onClick={onDismiss}>
              Dismiss
            </Button>
          </>
        ) : (
          <>
            <Button
              type="button"
              onClick={() => onDecision("allow", remember)}
              componentId="diagnostics.terminal.copy"
            >
              {remember ? "Allow copying" : "Allow for this session"}
            </Button>
            <Button
              type="button"
              variant="secondary"
              onClick={() => onDecision("once", false)}
              componentId="diagnostics.terminal.copy"
            >
              Copy once
            </Button>
            <Button type="button" variant="ghost" onClick={() => onDecision("block", remember)}>
              Block
            </Button>
          </>
        )}
      </div>
    </section>
  );
}
