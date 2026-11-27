// What a viewer sees in a gadget's place when they can't open it: it was deleted, or its owner
// stopped sharing it with them. The server answers both the same way (404), so the text names
// both, and a pinned gadget offers Unpin so the sidebar row can go.
import { PanelsTopLeftIcon, PinOffIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { GadgetErrorNotice } from "./GadgetErrorNotice";
import { isGadgetUnavailable, usePinGadget, useGadgetShelf } from "./gadgetQueries";

export function GadgetUnavailable({
  gadgetId,
  error,
  onRetry,
  className,
}: {
  gadgetId: string;
  /** Why it didn't open; a failure other than a missing gadget shows with Retry. */
  error: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  const shelf = useGadgetShelf();
  const pin = usePinGadget();
  if (!isGadgetUnavailable(error)) {
    return (
      <div className={cn("flex flex-col gap-4", className)}>
        <GadgetErrorNotice title="Couldn’t open the gadget" error={error} onRetry={onRetry} />
        <BackLink />
      </div>
    );
  }
  const pinned = shelf.data?.pinned.find((item) => item.id === gadgetId);
  return (
    <div
      role="status"
      data-testid="gadget-unavailable"
      className={cn(
        "flex min-h-60 flex-1 flex-col items-center justify-center gap-2 px-6 text-center",
        className,
      )}
    >
      <PanelsTopLeftIcon className="size-8 text-muted-foreground/50" />
      <p className="text-ui font-medium">
        {pinned ? `“${pinned.name}” isn’t available` : "This gadget isn’t available"}
      </p>
      <p className="max-w-sm text-sm text-muted-foreground">
        It was deleted, or you no longer have access to it. Ask its owner to share it with you
        again.
      </p>
      <div className="mt-2 flex flex-wrap items-center justify-center gap-2">
        {pinned && (
          <Button
            variant="outline"
            loading={pin.isPending}
            componentId="gadgets.unavailable.unpin"
            onClick={() => pin.mutate({ gadgetId, pinned: false })}
          >
            <PinOffIcon />
            Unpin from sidebar
          </Button>
        )}
        <Button asChild variant={pinned ? "ghost" : "outline"}>
          <Link to="/gadget">Back to your gadgets</Link>
        </Button>
      </div>
    </div>
  );
}

function BackLink() {
  return (
    <p className="text-ui text-muted-foreground">
      <Link to="/gadget" className="underline underline-offset-3 hover:text-foreground">
        Back to your gadgets
      </Link>
    </p>
  );
}
