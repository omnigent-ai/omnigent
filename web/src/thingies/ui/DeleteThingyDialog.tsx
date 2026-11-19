import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { Thingy } from "@/thingies/api/thingyApi";
import { ThingyErrorNotice } from "./ThingyErrorNotice";
import { useDeleteThingy } from "./thingyQueries";

/** Confirms deleting a thingy for everyone it is shared with. */
export function DeleteThingyDialog({
  thingy,
  onClose,
  onDeleted,
}: {
  thingy: Pick<Thingy, "id" | "name">;
  onClose: () => void;
  onDeleted?: () => void;
}) {
  const deleteThingy = useDeleteThingy();
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !deleteThingy.isPending) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete thingy?</DialogTitle>
          <DialogDescription>
            <span className="font-medium break-all">{thingy.name}</span>, its data and its versions
            will be deleted for everyone it is shared with. Sessions that worked on it are kept.
          </DialogDescription>
        </DialogHeader>
        {deleteThingy.isError && (
          <ThingyErrorNotice title="Couldn’t delete the thingy" error={deleteThingy.error} />
        )}
        <DialogFooter className="border-t-0 bg-transparent">
          <Button variant="ghost" onClick={onClose} disabled={deleteThingy.isPending}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            loading={deleteThingy.isPending}
            componentId="thingies.delete"
            onClick={() =>
              deleteThingy.mutate(thingy.id, {
                onSuccess: () => {
                  onClose();
                  onDeleted?.();
                },
              })
            }
          >
            Delete thingy
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
