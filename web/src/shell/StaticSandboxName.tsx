import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PencilIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { Host } from "@/hooks/useHosts";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

/** User-owned label shared by the New and Clone session pickers. */
export function StaticSandboxName({ host }: { host: Host }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const rename = useMutation({
    mutationFn: async () => {
      const response = await authenticatedFetch(`/v1/hosts/${encodeURIComponent(host.host_id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: name.trim() || null }),
      });
      if (!response.ok) throw await apiErrorFromResponse(response);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["hosts"] });
      setOpen(false);
    },
  });
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="size-7 shrink-0 text-muted-foreground"
        title="Rename static sandbox"
        aria-label="Rename static sandbox"
        onClick={() => {
          setName(host.display_name ?? "");
          rename.reset();
          setOpen(true);
        }}
      >
        <PencilIcon className="size-3.5" aria-hidden="true" />
      </Button>
      <Dialog open={open} onOpenChange={(next) => !rename.isPending && setOpen(next)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Rename static sandbox</DialogTitle>
            <DialogDescription>
              Choose a name to show in Omnigent. Leave blank to use {host.name}.
            </DialogDescription>
          </DialogHeader>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              event.stopPropagation();
              if (!rename.isPending) rename.mutate();
            }}
            className="flex flex-col gap-3"
          >
            <label className="flex flex-col gap-1 text-sm">
              Sandbox name
              <Input
                value={name}
                maxLength={64}
                disabled={rename.isPending}
                placeholder={host.name}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            {rename.isError && <p role="alert">{rename.error.message}</p>}
            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                disabled={rename.isPending}
                onClick={() => setOpen(false)}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={rename.isPending}>
                {rename.isPending ? "Saving…" : "Save"}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
