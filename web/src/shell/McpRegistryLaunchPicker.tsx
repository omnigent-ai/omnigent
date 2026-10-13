import { useCallback, useState } from "react";
import type { useMcpRegistryCatalog } from "@/hooks/useMcpRegistry";
import { ChevronDownIcon, PlugIcon } from "lucide-react";
import { McpRegistryPicker } from "@/components/McpRegistry";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

/** Launch-time selection of server-configured services; credentials stay on the server. */
export function McpRegistryLaunchPicker({
  selected,
  catalog,
  onToggle,
  disabled,
  onAuthorizingChange,
}: {
  selected: string[];
  catalog: ReturnType<typeof useMcpRegistryCatalog>;
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
  const selectedServices = catalog.services.filter((service) => selected.includes(service.id));
  const selectedCount = selectedServices.length;
  const needsSignInCount = selectedServices.filter((service) => !service.connected).length;
  return (
    <Popover open={open} onOpenChange={(next) => !authorizing && setOpen(next)}>
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          title={
            needsSignInCount ? `${needsSignInCount} MCP services need sign-in` : "MCP services"
          }
          aria-label={`MCP services: ${selectedCount} selected${needsSignInCount ? `, ${needsSignInCount} needs sign-in` : ""}`}
          data-testid="new-chat-landing-mcp-registry-chip"
          className="inline-flex h-6 shrink-0 items-center gap-1 rounded-md px-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-50"
        >
          <PlugIcon
            className={needsSignInCount ? "size-4 text-amber-600 dark:text-amber-500" : "size-4"}
          />
          <span className="hidden sm:inline">
            {selectedCount ? `MCPs · ${selectedCount}` : "MCPs"}
          </span>
          {selectedCount > 0 && <span className="sm:hidden">{selectedCount}</span>}
          {needsSignInCount > 0 && (
            <span className="hidden text-amber-600 sm:inline dark:text-amber-500">
              · {needsSignInCount} needs sign-in
            </span>
          )}
          <ChevronDownIcon className="hidden size-3.5 opacity-60 sm:block" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="flex max-h-[var(--radix-popover-content-available-height)] w-72 max-w-[calc(100vw-2rem)] flex-col p-0"
      >
        <div className="shrink-0 border-b border-border px-3 py-2.5">
          <h3 className="text-sm font-medium">Tools for this session</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            Turn on the server-configured MCP services this session should use. You’ll be asked to
            sign in when needed.
          </p>
        </div>
        <McpRegistryPicker
          attached={selected}
          catalog={catalog}
          onToggle={onToggle}
          busy={disabled}
          onAuthorizingChange={handleAuthorizingChange}
        />
      </PopoverContent>
    </Popover>
  );
}
