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

interface Sandbox {
  sandbox_id: string;
  name: string;
  state?: string | null;
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
  const inventory = useQuery({
    queryKey: ["sandbox-discovery", provider],
    queryFn: async (): Promise<Sandbox[]> => {
      const response = await authenticatedFetch(
        `/v1/hosts/sandboxes?provider=${encodeURIComponent(provider)}`,
      );
      if (!response.ok) throw new Error("Could not load static sandboxes. Try again.");
      return (await response.json()).sandboxes;
    },
    retry: false,
  });

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
              {box.state && (
                <span className="text-xs text-muted-foreground">Controller state: {box.state}</span>
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
