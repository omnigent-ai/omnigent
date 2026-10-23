import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { authenticatedFetch } from "@/lib/identity";
import type { Host } from "@/hooks/useHosts";
import { useViewerId } from "@/hooks/useViewerId";
import { cn } from "@/lib/utils";

interface Sandbox {
  sandbox_id: string;
  name: string;
  state?: string | null;
}

function useSandboxInventory(provider: string | null, enabled = true) {
  const viewer = useViewerId();
  return useQuery({
    queryKey: ["sandbox-discovery", viewer, provider],
    queryFn: async ({ signal }): Promise<Sandbox[]> => {
      const response = await authenticatedFetch(
        `/v1/hosts/sandboxes?provider=${encodeURIComponent(provider!)}`,
        { signal: AbortSignal.any([signal, AbortSignal.timeout(5_000)]) },
      );
      if (!response.ok) throw new Error("Could not load static sandboxes. Try again.");
      return (await response.json()).sandboxes;
    },
    enabled: enabled && provider !== null && viewer !== null,
    staleTime: 30_000,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
}

/** Optional controller information, never a gate on selecting or waking a host. */
export function StaticSandboxState({
  host,
  enabled,
  className,
}: {
  host: Host;
  enabled: boolean;
  className?: string;
}) {
  const provider =
    host.sandbox_attached && host.sandbox_id ? (host.sandbox_provider ?? null) : null;
  const inventory = useSandboxInventory(provider, enabled);
  if (!enabled || provider === null) return null;
  const labelClass = cn(
    "min-w-0 truncate text-xs font-normal leading-4 text-muted-foreground first-letter:uppercase",
    className,
  );
  if (inventory.isError) {
    return <span className={labelClass}>State unavailable</span>;
  }
  const state = inventory.data?.find((box) => box.sandbox_id === host.sandbox_id)?.state;
  if (!state) return null;
  return (
    <span
      className={labelClass}
      title={`Controller state: ${state}. Checked ${new Date(inventory.dataUpdatedAt).toLocaleString()}. Separate from host connectivity.`}
    >
      {inventory.isStale && "Last reported: "}
      {state}
    </span>
  );
}

/** Mounted only while choosing a box from one provider's current inventory. */
export function StaticSandboxPicker({
  provider,
  onClose,
  onSelect,
}: {
  provider: string;
  onClose: () => void;
  onSelect: (hostId: string) => void;
}) {
  const queryClient = useQueryClient();
  const [attaching, setAttaching] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inventory = useSandboxInventory(provider);

  async function attach(box: Sandbox) {
    setAttaching(box.sandbox_id);
    setError(null);
    try {
      const response = await authenticatedFetch("/v1/hosts/attach", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider,
          sandbox_id: box.sandbox_id,
          name: box.name.trim().slice(0, 64).trim() || "Static sandbox",
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Could not connect to static sandbox");
      await queryClient.invalidateQueries({ queryKey: ["hosts"] });
      void queryClient.invalidateQueries({ queryKey: ["sandbox-discovery"] });
      onSelect(body.host_id);
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not connect to static sandbox");
    } finally {
      setAttaching(null);
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && !attaching && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Choose existing static sandbox</DialogTitle>
          <DialogDescription>
            Connect to a sandbox you have access to, including one created outside Omnigent.
          </DialogDescription>
        </DialogHeader>
        {inventory.isPending && <p role="status">Loading static sandboxes…</p>}
        {inventory.isError && <p role="alert">{inventory.error.message}</p>}
        {error && <p role="alert">{error}</p>}
        {inventory.isSuccess && inventory.data.length === 0 && (
          <p>No existing static sandboxes available.</p>
        )}
        <div className="flex max-h-80 flex-col gap-2 overflow-y-auto">
          {inventory.data?.map((box) => (
            <Button
              key={box.sandbox_id}
              variant="outline"
              className="h-auto flex-col items-start whitespace-normal text-left"
              disabled={attaching !== null}
              onClick={() => void attach(box)}
            >
              <span className="break-all">{box.name}</span>
              <span className="break-all text-xs text-muted-foreground">{box.sandbox_id}</span>
              {box.state && !inventory.isError && (
                <span className="text-xs text-muted-foreground">
                  {inventory.isStale ? "Last reported controller state" : "Controller state"}:{" "}
                  {box.state}
                </span>
              )}
            </Button>
          ))}
        </div>
        {attaching && <p role="status">Connecting to static sandbox…</p>}
        <Button
          variant="ghost"
          disabled={attaching !== null || inventory.isFetching}
          onClick={() => void inventory.refetch()}
        >
          Refresh
        </Button>
      </DialogContent>
    </Dialog>
  );
}
