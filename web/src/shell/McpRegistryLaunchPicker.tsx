import { useCallback, useState } from "react";
import { ChevronDownIcon, PlugIcon } from "lucide-react";
import { McpRegistryPicker } from "@/components/McpRegistry";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

/** Launch-time selection of server-configured services; credentials stay on the server. */
export function McpRegistryLaunchPicker({
  selected,
  onToggle,
  disabled,
  onAuthorizingChange,
}: {
  selected: string[];
  onToggle: (id: string, enabled: boolean) => void;
  disabled: boolean;
  onAuthorizingChange: (pending: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [authorizing, setAuthorizing] = useState(false);
  const handleAuthorizingChange = useCallback(
    (pending: boolean) => {
      setAuthorizing(pending);
      onAuthorizingChange(pending);
    },
    [onAuthorizingChange],
  );
  return (
    <Popover open={open} onOpenChange={(next) => !authorizing && setOpen(next)}>
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          title="MCP services"
          aria-label={`MCP services: ${selected.length} selected`}
          data-testid="new-chat-landing-mcp-registry-chip"
          className="inline-flex h-6 shrink-0 items-center gap-1 rounded-md px-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-50"
        >
          <PlugIcon className="size-4" />
          <span className="hidden sm:inline">
            {selected.length ? `MCPs · ${selected.length}` : "MCPs"}
          </span>
          {selected.length > 0 && <span className="sm:hidden">{selected.length}</span>}
          <ChevronDownIcon className="hidden size-3.5 opacity-60 sm:block" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="max-h-[var(--radix-popover-content-available-height)] w-80 max-w-[calc(100vw-2rem)] overflow-y-auto p-3"
      >
        <h3 className="mb-2 text-sm font-medium">Tools for this session</h3>
        <McpRegistryPicker
          attached={selected}
          onToggle={onToggle}
          busy={disabled}
          onAuthorizingChange={handleAuthorizingChange}
        />
      </PopoverContent>
    </Popover>
  );
}
