import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
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

/** The row handles keyboard editing; pointer editing must not select or wake it. */
export function StaticSandboxEdit({ onEdit }: { onEdit: () => void }) {
  return (
    <span
      data-sandbox-edit=""
      aria-hidden="true"
      className="ml-auto flex h-6 shrink-0 cursor-pointer items-center text-xs font-normal text-muted-foreground underline-offset-2 opacity-0 hover:text-foreground hover:underline group-hover/sandbox:opacity-100 group-focus/sandbox:opacity-100 group-data-[active=true]/sandbox:opacity-100 group-data-[state=checked]/sandbox:opacity-100 max-md:opacity-100"
      onPointerUp={(event) => event.preventDefault()}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onEdit();
      }}
    >
      Edit
    </span>
  );
}

/** Mounted outside the picker so closing its menu does not dismiss the dialog. */
export function StaticSandboxName({
  host,
  onClose,
  onCloseAutoFocus,
}: {
  host: Host;
  onClose: () => void;
  onCloseAutoFocus?: (event: Event) => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(host.display_name ?? "");
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
      onClose();
    },
  });
  return (
    <Dialog open onOpenChange={(next) => !next && !rename.isPending && onClose()}>
      <DialogContent onCloseAutoFocus={onCloseAutoFocus}>
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
            <Button type="button" variant="outline" disabled={rename.isPending} onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={rename.isPending}>
              {rename.isPending ? "Saving…" : "Save"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
