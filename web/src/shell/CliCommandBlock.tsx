import { useCallback, useEffect, useRef, useState } from "react";
import { CheckIcon, CopyIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Copy `text` to the clipboard and flash a "copied" state for 2s.
 * Shared by the block and inline CLI-command surfaces.
 */
function useCopy(text: string) {
  const [copied, setCopied] = useState(false);
  const copyTimeoutRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (copyTimeoutRef.current !== null) {
        window.clearTimeout(copyTimeoutRef.current);
      }
    };
  }, []);

  const copy = useCallback(async () => {
    if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      return;
    }
    setCopied(true);
    if (copyTimeoutRef.current !== null) window.clearTimeout(copyTimeoutRef.current);
    copyTimeoutRef.current = window.setTimeout(() => setCopied(false), 2000);
  }, [text]);

  return { copied, copy };
}

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
  const { copied, copy } = useCopy(command);

  return (
    // items-start: copy button anchors to top-right next to the first
    // line when the command wraps.
    // break-all: force-wraps tokens that have no whitespace (long URLs
    // are the common case in the resume-runner dialog). `break-words`
    // is too gentle — it only breaks a word when it sits alone on a
    // line, which doesn't fire if surrounding tokens push it to wrap.
    <div className="flex w-full items-center gap-2 rounded-md border border-border bg-muted px-3 py-2 font-mono text-sm">
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
        onClick={() => void copy()}
        className="shrink-0"
      >
        {copied ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}
      </Button>
    </div>
  );
}

/**
 * Inline `<code>` with a small copy button, for CLI commands mentioned
 * mid-sentence (e.g. inside an error message). Selectable text plus a
 * one-click copy, without the full block treatment.
 */
export function InlineCliCode({ command }: { command: string }) {
  const { copied, copy } = useCopy(command);

  return (
    <span className="inline-flex items-baseline gap-1 rounded bg-muted px-1 align-baseline font-mono">
      <code className="select-text break-all [font-variant-ligatures:none] [font-feature-settings:'liga'_0,'calt'_0]">
        {command}
      </code>
      <button
        type="button"
        aria-label={copied ? "Copied" : "Copy command"}
        onClick={() => void copy()}
        className={cn("shrink-0 self-center opacity-70 hover:opacity-100", copied && "opacity-100")}
      >
        {copied ? <CheckIcon className="size-3" /> : <CopyIcon className="size-3" />}
      </button>
    </span>
  );
}

/**
 * Render `text`, turning `` `backtick` ``-wrapped spans into
 * {@link InlineCliCode}. Odd segments of the split are the code spans.
 */
export function renderTextWithInlineCode(text: string) {
  return text.split("`").map((segment, i) => {
    // Static text → keys are stable across renders; index disambiguates
    // repeated segments (e.g. the same command mentioned twice).
    const key = `${i}:${segment}`;
    return i % 2 === 1 ? (
      <InlineCliCode key={key} command={segment} />
    ) : (
      <span key={key}>{segment}</span>
    );
  });
}
