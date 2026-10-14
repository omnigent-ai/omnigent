import { useCallback, useState } from "react";
import type { useMcpRegistryCatalog } from "@/hooks/useMcpRegistry";
import { ChevronDownIcon, PlugIcon } from "lucide-react";
import { McpRegistryPicker } from "@/components/McpRegistry";
import { COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS } from "@/components/composer/ChatComposer";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

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
  const selectedCount =
    catalog.loading || catalog.error ? selected.length : selectedServices.length;
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
          className="relative inline-flex h-6 min-w-10 max-w-[calc(50%-0.25rem)] cursor-pointer items-center gap-1 rounded-md border border-transparent bg-transparent px-0.5 text-xs leading-4 font-normal text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:cursor-default disabled:opacity-50 md:min-w-11 md:px-1"
        >
          <PlugIcon
            className={cn(
              "size-4 shrink-0",
              needsSignInCount && "text-amber-600 dark:text-amber-500",
            )}
          />
          <span
            data-workspace-collapse-label=""
            className={cn("min-w-0 truncate text-left", COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS)}
          >
            {selectedCount ? `MCPs · ${selectedCount}` : "MCPs"}
          </span>
          {needsSignInCount > 0 && (
            <span className="truncate text-amber-600 dark:text-amber-500">
              · {needsSignInCount} needs sign-in
            </span>
          )}
          <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        collisionPadding={12}
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
