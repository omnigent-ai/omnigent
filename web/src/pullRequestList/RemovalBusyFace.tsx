// A busy Remove control's face while it removes: a spinner and how far it got. Where the
// removal can be stopped, pointing at or focusing the control swaps the spinner for Stop's X.

import { Loader2Icon, XIcon } from "lucide-react";

export function RemovalBusyFace({ label, stoppable }: { label: string; stoppable: boolean }) {
  return (
    <>
      <Loader2Icon className="pr-list-spin pr-list-busy-spinner" aria-hidden />
      {stoppable && <XIcon className="pr-list-busy-stop" aria-hidden />}
      <span className="pr-list-busy-label">{label}</span>
    </>
  );
}
