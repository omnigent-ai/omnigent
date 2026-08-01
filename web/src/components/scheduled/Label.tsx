// Small shared form label for the scheduled-tasks forms. The repo has no shadcn
// `label` primitive; other forms (RegisterPage) hand-roll a `<label>` with
// `text-sm font-medium leading-none`, so this centralizes that one style for the
// several fields the create dialog renders.

import type { ComponentPropsWithoutRef } from "react";
import { cn } from "@/lib/utils";

export function Label({ className, ...props }: ComponentPropsWithoutRef<"label">) {
  return (
    // eslint-disable-next-line jsx-a11y/label-has-associated-control -- callers pass htmlFor
    <label className={cn("text-sm font-medium leading-none", className)} {...props} />
  );
}
