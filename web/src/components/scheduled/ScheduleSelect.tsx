import { useState, type ComponentProps } from "react";
import { Select } from "@/components/ui/select";
import { useScheduleEscape } from "./useScheduleEscape";

/** Consume Escape before the automation dialog can dismiss unsaved edits. */
export function ScheduleSelect({
  onOpenChange,
  ...props
}: Omit<ComponentProps<typeof Select>, "open" | "defaultOpen">) {
  const [open, setOpen] = useState(false);
  function changeOpen(next: boolean) {
    setOpen(next);
    onOpenChange?.(next);
  }
  useScheduleEscape(open, () => {
    changeOpen(false);
    return true;
  });
  return <Select {...props} open={open} onOpenChange={changeOpen} />;
}
