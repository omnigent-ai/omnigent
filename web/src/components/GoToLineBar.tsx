// Floating Go to line box for a file viewer. The reader types a line, or
// line:column; the viewer previews it as they type and jumps there on Enter.

import { useEffect, useId, useRef, useState } from "react";
import { CornerDownLeftIcon, HashIcon } from "lucide-react";
import { ENTER_KEY, KeyboardShortcutTooltipContent } from "@/components/KeyboardShortcut";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from "@/components/ui/input-group";
import { Tooltip, TooltipTrigger } from "@/components/ui/tooltip";
import { type LineTarget, parseLineTarget } from "@/lib/lineTarget";
import { cn } from "@/lib/utils";

export const GO_TO_LINE_LABEL = "Go to line";

export interface GoToLineBarProps {
  lineCount: number;
  /** The line the cursor is on, shown as a starting point. */
  currentLine: number;
  /** Shows a line while the reader types; null when the text names none. */
  onPreview: (target: LineTarget | null) => void;
  onGo: (target: LineTarget) => void;
  /** Escape, which puts the view back where it was. */
  onCancel: () => void;
  /** Focus left the box for somewhere else, which keeps the view where it is. */
  onDismiss: () => void;
  /** Changes on every open request so the field is focused and selected again. */
  focusRequest: unknown;
  className?: string;
}

export function GoToLineBar({
  lineCount,
  currentLine,
  onPreview,
  onGo,
  onCancel,
  onDismiss,
  focusRequest,
  className,
}: GoToLineBarProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const hintId = useId();
  const [text, setText] = useState("");
  const parsed = parseLineTarget(text, lineCount);
  const lines = lineCount.toLocaleString();

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [focusRequest]);

  const hint =
    parsed.kind === "invalid"
      ? `Type a line number from 1 to ${lines}, or line:column.`
      : parsed.kind === "target"
        ? `Go to line ${parsed.line.toLocaleString()}${parsed.column ? `, column ${parsed.column}` : ""} of ${lines}.`
        : `Current line ${currentLine.toLocaleString()} of ${lines}. Type a line number, or line:column.`;
  const go = () => {
    if (parsed.kind === "target") onGo(parsed);
  };

  return (
    <form
      aria-label={GO_TO_LINE_LABEL}
      onSubmit={(event) => {
        event.preventDefault();
        go();
      }}
      onKeyDown={(event) => {
        if (event.key !== "Escape" || event.nativeEvent.isComposing) return;
        event.preventDefault();
        event.stopPropagation();
        onCancel();
      }}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) onDismiss();
      }}
      className={cn(
        "pointer-events-auto w-80 max-w-full rounded-lg border border-border bg-popover text-popover-foreground shadow-md",
        className,
      )}
    >
      <InputGroup className="border-0 dark:bg-transparent">
        <InputGroupAddon>
          <HashIcon aria-hidden="true" />
        </InputGroupAddon>
        <InputGroupInput
          ref={inputRef}
          value={text}
          placeholder={`${GO_TO_LINE_LABEL} (1–${lines})`}
          aria-label={GO_TO_LINE_LABEL}
          aria-describedby={hintId}
          aria-invalid={parsed.kind === "invalid" || undefined}
          inputMode="numeric"
          spellCheck={false}
          autoComplete="off"
          data-testid="go-to-line-input"
          onChange={(event) => {
            const next = parseLineTarget(event.target.value, lineCount);
            setText(event.target.value);
            onPreview(next.kind === "target" ? next : null);
          }}
        />
        <InputGroupAddon align="inline-end">
          <Tooltip>
            <TooltipTrigger asChild>
              <InputGroupButton
                type="submit"
                size="icon-xs"
                aria-label="Go"
                disabled={parsed.kind !== "target"}
              >
                <CornerDownLeftIcon />
              </InputGroupButton>
            </TooltipTrigger>
            <KeyboardShortcutTooltipContent label="Go" keys={[ENTER_KEY]} />
          </Tooltip>
        </InputGroupAddon>
      </InputGroup>
      <p
        id={hintId}
        aria-live="polite"
        className={cn(
          "border-t border-border px-3 py-1.5 text-xs text-muted-foreground",
          parsed.kind === "invalid" && "text-destructive",
        )}
      >
        {hint}
      </p>
    </form>
  );
}
