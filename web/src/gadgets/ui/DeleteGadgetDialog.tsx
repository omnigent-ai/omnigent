import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { Gadget } from "@/gadgets/api/gadgetApi";
import { GadgetErrorNotice } from "./GadgetErrorNotice";
import { useDeleteGadget } from "./gadgetQueries";

/** Confirms deleting a gadget for everyone it is shared with. */
export function DeleteGadgetDialog({
  gadget,
  onClose,
  onDeleted,
  onCloseAutoFocus,
}: {
  gadget: Pick<Gadget, "id" | "name">;
  onClose: () => void;
  onDeleted?: () => void;
  /** Where focus goes once the dialog closes; Radix's default is the element that opened it. */
  onCloseAutoFocus?: (event: Event) => void;
}) {
  const deleteGadget = useDeleteGadget();
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !deleteGadget.isPending) onClose();
      }}
    >
      <DialogContent onCloseAutoFocus={onCloseAutoFocus}>
        <DialogHeader>
          <DialogTitle>Delete gadget?</DialogTitle>
          <DialogDescription>
            “<span className="font-semibold break-all text-foreground">{gadget.name}</span>”, its
            data and its versions will be deleted for everyone it is shared with. Sessions that
            worked on it are kept.
          </DialogDescription>
        </DialogHeader>
        {deleteGadget.isError && (
          <GadgetErrorNotice title="Couldn’t delete the gadget" error={deleteGadget.error} />
        )}
        <DialogFooter className="border-t-0 bg-transparent">
          <Button variant="ghost" onClick={onClose} disabled={deleteGadget.isPending}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            loading={deleteGadget.isPending}
            componentId="gadgets.delete"
            onClick={() =>
              deleteGadget.mutate(gadget.id, {
                onSuccess: () => {
                  onClose();
                  onDeleted?.();
                },
              })
            }
          >
            Delete gadget
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
