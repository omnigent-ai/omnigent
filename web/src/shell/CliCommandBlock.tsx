import { useEffect, useRef, useState } from "react";
import { CheckIcon, CopyIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

/**
 * Code box with a copy-to-clipboard button — used by every "go run
 * this CLI command" surface in the web UI (new-chat dialog, the `/`
 * landing screen, the resume-runner dialog).
 *
 * `testIdPrefix` namespaces the `data-testid`s on the code and copy
 * button so each caller can assert against its own surface — e.g.
 * `"new-chat"` produces `new-chat-command` / `new-chat-copy`.
 */
export function CliCommandBlock({
  command,
  testIdPrefix,
}: {
  command: string;
  testIdPrefix: string;
}) {
  const [copied, setCopied] = useState(false);
  const copyTimeoutRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (copyTimeoutRef.current !== null) {
        window.clearTimeout(copyTimeoutRef.current);
      }
    };
  }, []);

  async function copyCommand() {
    if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) return;
    try {
      await navigator.clipboard.writeText(command);
    } catch {
      return;
    }
    setCopied(true);
    if (copyTimeoutRef.current !== null) window.clearTimeout(copyTimeoutRef.current);
    copyTimeoutRef.current = window.setTimeout(() => setCopied(false), 2000);
  }

  return (
    // items-start: copy button anchors to top-right next to the first
    // line when the command wraps.
    // break-all: force-wraps tokens that have no whitespace (long URLs
    // are the common case in the resume-runner dialog). `break-words`
    // is too gentle — it only breaks a word when it sits alone on a
    // line, which doesn't fire if surrounding tokens push it to wrap.
    <div className="flex w-full items-center gap-2 rounded-md border border-border bg-muted px-3 py-2 font-mono text-xs">
      <code
        className="min-w-0 flex-1 break-all whitespace-pre-wrap [font-variant-ligatures:none] [font-feature-settings:'liga'_0,'calt'_0]"
        data-testid={`${testIdPrefix}-command`}
      >
        {command}
      </code>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label={copied ? "Copied" : "Copy command"}
        data-testid={`${testIdPrefix}-copy`}
        onClick={copyCommand}
        className="shrink-0"
      >
        {copied ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}
      </Button>
    </div>
  );
}
