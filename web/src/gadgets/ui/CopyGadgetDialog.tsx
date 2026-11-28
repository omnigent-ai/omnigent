import { type FormEvent, useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { Gadget } from "@/gadgets/api/gadgetApi";
import { gadgetBoardPath } from "@/gadgets/host/gadgetPages";
import { useNavigate } from "@/lib/routing";
import { GadgetErrorNotice } from "./GadgetErrorNotice";
import { MAX_GADGET_NAME_LENGTH } from "./GadgetTemplateGallery";
import { IN_APP_GADGET_OPEN, gadgetErrorMessage } from "./gadgetNavigation";
import { useCopyGadget } from "./gadgetQueries";

/** Makes a copy the viewer owns of a published gadget, then opens it. */
export function CopyGadgetDialog({
  gadget,
  onClose,
  onCloseAutoFocus,
}: {
  gadget: Pick<Gadget, "id" | "name">;
  onClose: () => void;
  /** Where focus goes once the dialog closes; Radix's default is the element that opened it. */
  onCloseAutoFocus?: (event: Event) => void;
}) {
  const copy = useCopyGadget();
  const navigate = useNavigate();
  const [name, setName] = useState(`Copy of ${gadget.name}`.slice(0, MAX_GADGET_NAME_LENGTH));
  const [includeData, setIncludeData] = useState(false);
  const nameId = useId();
  const dataId = useId();
  const trimmed = name.trim();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!trimmed || copy.isPending) return;
    copy.mutate(
      { gadgetId: gadget.id, name: trimmed, includeData },
      {
        onSuccess: (copied) => {
          onClose();
          navigate(gadgetBoardPath(copied.id), { state: IN_APP_GADGET_OPEN });
        },
      },
    );
  };
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !copy.isPending) onClose();
      }}
    >
      <DialogContent className="sm:max-w-md" onCloseAutoFocus={onCloseAutoFocus}>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>Make a copy</DialogTitle>
            <DialogDescription>
              A gadget of your own with this one’s published version. Comments and sharing stay with
              the original, and later changes to either don’t reach the other.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={nameId} className="text-sm font-medium text-muted-foreground">
              Name
            </label>
            <Input
              id={nameId}
              value={name}
              autoFocus
              maxLength={MAX_GADGET_NAME_LENGTH}
              onFocus={(event) => event.currentTarget.select()}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id={dataId}
              checked={includeData}
              componentId="gadgets.copy.include_data"
              onCheckedChange={(checked) => setIncludeData(checked === true)}
            />
            <label htmlFor={dataId} className="text-sm">
              Copy its data too
            </label>
          </div>
          {copy.isError && (
            <GadgetErrorNotice
              title="Couldn’t copy the gadget"
              error={gadgetErrorMessage(copy.error)}
            />
          )}
          <DialogFooter className="border-t-0 bg-transparent">
            <Button type="button" variant="ghost" onClick={onClose} disabled={copy.isPending}>
              Cancel
            </Button>
            <Button
              type="submit"
              loading={copy.isPending}
              disabled={!trimmed}
              componentId="gadgets.copy"
            >
              Make a copy
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
