// Titled code panels, the expandable output preview, and the error box shared by tool-call bodies.

import { CheckIcon, CopyIcon, Maximize2Icon, Minimize2Icon, WrapTextIcon } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CodeBlock,
  CodeBlockActions,
  type CodeLanguage,
  CodeBlockHeader,
  CodeBlockTitle,
} from "@/components/ai-elements/code-block";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/** A tool call's failure message. */
/** Diff colors for added and removed line counts, as in the changed-files diffstat. */
export const DIFF_TONE_CLASS = {
  added: "text-green-600 dark:text-green-400",
  removed: "text-red-600 dark:text-red-400",
} as const;

export function ToolErrorBox({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        "max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-destructive text-sm",
        className,
      )}
      data-testid="tool-error-box"
    >
      {children}
    </div>
  );
}

export function CodePanel({
  title,
  text,
  copyText,
  copyLabel,
  language = "json",
}: {
  title?: ReactNode;
  text: string;
  copyText: string;
  copyLabel: string;
  language?: CodeLanguage;
}) {
  // Soft-wrap long lines by default so the panel never needs horizontal
  // scrolling to read; the toggle restores the scrolling view for when
  // column alignment matters (same affordance as chat code blocks).
  const [wrap, setWrap] = useState(true);
  return (
    <CodeBlock code={text} language={language} wrap={wrap}>
      <CodeBlockHeader>
        <CodeBlockTitle className="min-w-0">
          {title !== undefined && <span className="truncate font-medium">{title}</span>}
        </CodeBlockTitle>
        <CodeBlockActions>
          <WrapTextButton onToggle={() => setWrap((value) => !value)} wrap={wrap} />
          <CopyTextButton label={copyLabel} text={copyText} />
        </CodeBlockActions>
      </CodeBlockHeader>
    </CodeBlock>
  );
}

export function WrapTextButton({ wrap, onToggle }: { wrap: boolean; onToggle: () => void }) {
  const label = wrap ? "Disable word wrap" : "Enable word wrap";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={label}
          aria-pressed={wrap}
          className={cn("size-6 text-muted-foreground", wrap && "text-foreground")}
          onClick={onToggle}
          size="icon-xs"
          type="button"
          variant="ghost"
          componentId="diagnostics.tool.toggle_wrap"
        >
          <WrapTextIcon className="size-3.5" />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

interface CopyTextButtonProps {
  text: string;
  label: string;
  /** Visible text next to the icon, for when several copy buttons sit together. */
  caption?: string;
}

export function CopyTextButton({ text, label, caption }: CopyTextButtonProps) {
  const [isCopied, setIsCopied] = useState(false);
  const timeoutRef = useRef<number | null>(null);

  const copyToClipboard = useCallback(async () => {
    if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) {
      return;
    }

    try {
      await navigator.clipboard.writeText(text);
    } catch {
      return;
    }

    setIsCopied(true);
    if (timeoutRef.current !== null) {
      window.clearTimeout(timeoutRef.current);
    }
    timeoutRef.current = window.setTimeout(() => setIsCopied(false), 2000);
  }, [text]);

  useEffect(
    () => () => {
      if (timeoutRef.current !== null) {
        window.clearTimeout(timeoutRef.current);
      }
    },
    [],
  );

  const Icon = isCopied ? CheckIcon : CopyIcon;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={isCopied ? "Copied" : label}
          className={cn("text-muted-foreground", caption ? "h-6 gap-1 px-1.5" : "size-6")}
          onClick={copyToClipboard}
          size={caption ? "xs" : "icon-xs"}
          type="button"
          variant="ghost"
          componentId="diagnostics.tool.copy"
        >
          <Icon className="size-3.5" />
          {caption}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{isCopied ? "Copied" : label}</TooltipContent>
    </Tooltip>
  );
}

const OUTPUT_PREVIEW_LINE_LIMIT = 80;
const OUTPUT_PREVIEW_CHAR_LIMIT = 12_000;

interface OutputPreview {
  text: string;
  isTruncated: boolean;
  lineCount: number;
  charCount: number;
  shownLineCount: number;
  shownCharCount: number;
  hiddenLineCount: number;
  hiddenCharCount: number;
}

export function getOutputPreview(output: string, expanded = false): OutputPreview {
  const lines = output.length === 0 ? [] : output.split("\n");
  const lineCount = lines.length;
  const charCount = output.length;

  if (
    expanded ||
    (lineCount <= OUTPUT_PREVIEW_LINE_LIMIT && charCount <= OUTPUT_PREVIEW_CHAR_LIMIT)
  ) {
    return {
      text: output,
      isTruncated: false,
      lineCount,
      charCount,
      shownLineCount: lineCount,
      shownCharCount: charCount,
      hiddenLineCount: 0,
      hiddenCharCount: 0,
    };
  }

  let text =
    lineCount > OUTPUT_PREVIEW_LINE_LIMIT
      ? lines.slice(0, OUTPUT_PREVIEW_LINE_LIMIT).join("\n")
      : output;

  if (text.length > OUTPUT_PREVIEW_CHAR_LIMIT) {
    text = text.slice(0, OUTPUT_PREVIEW_CHAR_LIMIT).trimEnd();
  }

  const shownLineCount = text.length === 0 ? 0 : text.split("\n").length;
  const shownCharCount = text.length;

  return {
    text,
    isTruncated: shownCharCount < charCount,
    lineCount,
    charCount,
    shownLineCount,
    shownCharCount,
    hiddenLineCount: Math.max(0, lineCount - shownLineCount),
    hiddenCharCount: Math.max(0, charCount - shownCharCount),
  };
}

export function OutputSection({
  output,
  title,
  renderPanel,
  language,
}: {
  output: string;
  /** The default panel's title; the Fields view leaves it out, since its section label names it. */
  title?: string;
  /** Renders the (possibly truncated) text; defaults to a code panel. */
  renderPanel?: (text: string) => ReactNode;
  /** Highlighting for the default panel; JSON unless set. */
  language?: CodeLanguage;
}) {
  const [isExpanded, setIsExpanded] = useState(false);
  useEffect(() => setIsExpanded(false), [output]);

  const collapsedPreview = useMemo(() => getOutputPreview(output), [output]);
  const preview = useMemo(() => getOutputPreview(output, isExpanded), [output, isExpanded]);
  const canExpand = collapsedPreview.isTruncated;

  return (
    <div className="space-y-2">
      <div
        className={cn(
          "relative rounded-md",
          canExpand && !isExpanded && "max-h-80 overflow-hidden",
          // overflow-auto (vs overflow-y-auto) keeps long single-line output from blowing out the bubble width.
          (!canExpand || isExpanded) && "max-h-[36rem] overflow-auto",
        )}
      >
        {renderPanel ? (
          renderPanel(preview.text)
        ) : (
          <CodePanel
            title={title}
            text={preview.text}
            copyText={output}
            copyLabel="Copy result"
            language={language}
          />
        )}
        {canExpand && !isExpanded && (
          <div className="pointer-events-none absolute inset-x-px bottom-px h-16 rounded-b-md bg-gradient-to-t from-background to-transparent" />
        )}
      </div>
      {canExpand && (
        <div className="flex flex-col gap-2 rounded-md border bg-muted/30 px-3 py-2 text-muted-foreground text-sm sm:flex-row sm:items-center sm:justify-between">
          <span className="min-w-0">
            {isExpanded ? "Showing full output" : "Previewing output"} (
            {formatOutputStats(isExpanded ? preview : collapsedPreview)})
          </span>
          <Button
            className="w-fit"
            onClick={() => setIsExpanded((value) => !value)}
            size="xs"
            type="button"
            variant="outline"
            componentId="diagnostics.tool.toggle_expanded"
          >
            {isExpanded ? (
              <Minimize2Icon className="size-3" />
            ) : (
              <Maximize2Icon className="size-3" />
            )}
            {isExpanded ? "Collapse" : "Expand"}
          </Button>
        </div>
      )}
    </div>
  );
}

function formatOutputStats(preview: OutputPreview): string {
  if (!preview.isTruncated) {
    return `${formatCount(preview.lineCount, "line")} / ${formatCount(preview.charCount, "char")}`;
  }

  const hidden: string[] = [];
  if (preview.hiddenLineCount > 0) {
    hidden.push(`${formatCount(preview.hiddenLineCount, "line")} hidden`);
  }
  if (preview.hiddenCharCount > 0) {
    hidden.push(`${formatCount(preview.hiddenCharCount, "char")} hidden`);
  }

  return `${formatCount(preview.shownLineCount, "line")} / ${formatCount(
    preview.shownCharCount,
    "char",
  )} shown; ${hidden.join(", ")}`;
}

function formatCount(count: number, unit: string): string {
  return `${count.toLocaleString()} ${unit}${count === 1 ? "" : "s"}`;
}
