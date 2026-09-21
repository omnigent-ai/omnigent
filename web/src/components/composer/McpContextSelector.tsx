import type { KeyboardEvent } from "react";
import {
  AlertCircleIcon,
  CheckIcon,
  ChevronDownIcon,
  LoaderCircleIcon,
  ServerIcon,
  XIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { ComposerContextResourceState, ComposerMcpSelection } from "@/lib/composerContext";
import { cn } from "@/lib/utils";
import type { McpContextOption } from "./mcpContextOptions";

export interface McpContextSelectorProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  resource: ComposerContextResourceState<readonly McpContextOption[]>;
  value: readonly ComposerMcpSelection[];
  onChange: (value: ComposerMcpSelection[]) => void;
  onRetry?: () => void;
  disabled?: boolean;
  className?: string;
}

function focusRelativeOption(event: KeyboardEvent<HTMLDivElement>) {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  const options = Array.from(
    event.currentTarget.querySelectorAll<HTMLButtonElement>("[data-mcp-option]:not(:disabled)"),
  );
  if (options.length === 0) return;
  const currentIndex = options.indexOf(event.target as HTMLButtonElement);
  let nextIndex = 0;
  if (event.key === "End") nextIndex = options.length - 1;
  else if (event.key === "ArrowDown") nextIndex = (currentIndex + 1) % options.length;
  else if (event.key === "ArrowUp")
    nextIndex = (currentIndex - 1 + options.length) % options.length;
  event.preventDefault();
  options[nextIndex]?.focus();
}

function SelectionChip({
  selection,
  onRemove,
  disabled,
}: {
  selection: ComposerMcpSelection;
  onRemove: () => void;
  disabled: boolean;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onRemove}
      aria-label={`Remove ${selection.serverName} from MCP context`}
      title={selection.serverName}
      className="flex h-7 min-w-0 max-w-48 items-center gap-1 rounded-full border border-border/70 bg-muted/70 px-2 text-xs text-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50"
    >
      <span className="truncate font-mono">{selection.serverName}</span>
      <XIcon className="size-3 shrink-0 text-muted-foreground" aria-hidden="true" />
    </button>
  );
}

function OptionRow({
  option,
  checked,
  unavailable = false,
  onSelect,
}: {
  option: McpContextOption;
  checked: boolean;
  unavailable?: boolean;
  onSelect: () => void;
}) {
  const detail = [option.transport, option.detail].filter(Boolean).join(" · ");
  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      data-mcp-option=""
      onClick={onSelect}
      className="group flex w-full min-w-0 items-center gap-2 rounded-md px-2 py-2 text-left text-ui outline-none transition-colors hover:bg-muted focus-visible:bg-muted focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring/40"
    >
      <span
        className={cn(
          "flex size-4 shrink-0 items-center justify-center rounded border border-border bg-background",
          checked && "border-foreground bg-foreground text-background",
        )}
        aria-hidden="true"
      >
        {checked && <CheckIcon className="size-3" />}
      </span>
      <ServerIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span className="min-w-0 flex-1">
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate font-mono text-[13px] font-medium">{option.serverName}</span>
          {unavailable && (
            <span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
              Unavailable
            </span>
          )}
        </span>
        <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
          <span className="shrink-0">{detail}</span>
          {option.description && (
            <>
              <span aria-hidden="true">·</span>
              <span className="truncate">{option.description}</span>
            </>
          )}
        </span>
      </span>
    </button>
  );
}

export function McpContextSelector({
  open,
  onOpenChange,
  resource,
  value,
  onChange,
  onRetry,
  disabled = false,
  className,
}: McpContextSelectorProps) {
  const options = resource.data ?? [];
  const selectedIds = new Set(value.map((selection) => selection.id));
  const availableIds = new Set(options.map((option) => option.id));
  const unavailableSelections = value.filter((selection) => !availableIds.has(selection.id));
  const selectionLabel =
    value.length === 0
      ? "No MCP context"
      : `${value.length} MCP server${value.length === 1 ? "" : "s"} selected`;

  function toggle(option: McpContextOption) {
    if (selectedIds.has(option.id)) {
      onChange(value.filter((selection) => selection.id !== option.id));
      return;
    }
    onChange([...value, { id: option.id, serverName: option.serverName }]);
  }

  return (
    <div className={cn("flex min-w-0 max-w-full flex-wrap items-center gap-1.5", className)}>
      <Popover open={open} onOpenChange={onOpenChange}>
        <PopoverTrigger asChild>
          <button
            type="button"
            disabled={disabled}
            aria-label={`Choose MCP context. ${selectionLabel}.`}
            className="flex h-8 shrink-0 items-center gap-1.5 rounded-lg border border-border/70 bg-background px-2.5 text-ui text-foreground shadow-xs transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50"
          >
            <ServerIcon className="size-3.5 text-muted-foreground" aria-hidden="true" />
            <span>MCP</span>
            <span className="text-xs tabular-nums text-muted-foreground">
              {value.length === 0 ? "None" : value.length}
            </span>
            <ChevronDownIcon className="size-3.5 text-muted-foreground" aria-hidden="true" />
          </button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-[min(24rem,calc(100vw-1rem))] gap-1 p-1.5">
          <div className="px-2 pt-1.5 pb-1">
            <p className="text-sm font-medium">MCP context</p>
            <p className="text-xs text-muted-foreground">
              Choose the servers whose context is available to the new session.
            </p>
          </div>
          <div
            role="group"
            aria-label="MCP context servers"
            onKeyDown={focusRelativeOption}
            className="max-h-72 overflow-y-auto"
          >
            <button
              type="button"
              role="checkbox"
              aria-checked={value.length === 0}
              data-mcp-option=""
              onClick={() => onChange([])}
              className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-ui outline-none transition-colors hover:bg-muted focus-visible:bg-muted focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring/40"
            >
              <span
                className={cn(
                  "flex size-4 shrink-0 items-center justify-center rounded border border-border bg-background",
                  value.length === 0 && "border-foreground bg-foreground text-background",
                )}
                aria-hidden="true"
              >
                {value.length === 0 && <CheckIcon className="size-3" />}
              </span>
              <span className="min-w-0">
                <span className="block text-[13px] font-medium">No MCP context</span>
                <span className="block truncate text-xs text-muted-foreground">
                  Start without adding server context
                </span>
              </span>
            </button>

            {resource.status === "loading" && (
              <div
                className="flex items-center gap-2 px-2 py-4 text-xs text-muted-foreground"
                role="status"
              >
                <LoaderCircleIcon className="size-3.5 animate-spin" aria-hidden="true" />
                Loading MCP servers…
              </div>
            )}
            {resource.status === "unavailable" && (
              <p className="px-2 py-4 text-xs text-muted-foreground">
                MCP context is unavailable for this agent.
              </p>
            )}
            {resource.status === "error" && (
              <div
                role="alert"
                className="m-1 flex items-start gap-2 rounded-md bg-destructive/10 p-2 text-xs text-destructive"
              >
                <AlertCircleIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
                <span className="min-w-0 flex-1">
                  <span className="block font-medium">Couldn’t load MCP servers</span>
                  <span className="block truncate opacity-80">{resource.error.message}</span>
                </span>
                {onRetry && (
                  <Button type="button" variant="ghost" size="xs" onClick={onRetry}>
                    Retry
                  </Button>
                )}
              </div>
            )}
            {(resource.status === "ready" ||
              resource.status === "stale" ||
              (resource.status === "error" && options.length > 0)) &&
              (options.length > 0 ? (
                options.map((option) => (
                  <OptionRow
                    key={option.id}
                    option={option}
                    checked={selectedIds.has(option.id)}
                    onSelect={() => toggle(option)}
                  />
                ))
              ) : (
                <p className="px-2 py-4 text-xs text-muted-foreground">
                  No MCP servers are configured for this agent.
                </p>
              ))}
            {unavailableSelections.map((selection) => (
              <OptionRow
                key={selection.id}
                option={{
                  id: selection.id,
                  serverName: selection.serverName,
                  description: null,
                  transport: "Saved selection",
                  detail: null,
                }}
                checked
                unavailable
                onSelect={() => onChange(value.filter((item) => item.id !== selection.id))}
              />
            ))}
          </div>
        </PopoverContent>
      </Popover>

      {value.length === 0 ? (
        <span className="flex h-7 items-center rounded-full bg-muted/60 px-2 text-xs text-muted-foreground">
          No MCP
        </span>
      ) : (
        value.map((selection) => (
          <SelectionChip
            key={selection.id}
            selection={selection}
            disabled={disabled}
            onRemove={() => onChange(value.filter((item) => item.id !== selection.id))}
          />
        ))
      )}
    </div>
  );
}
