import type { ReactNode } from "react";

export const selectClass = "h-9 w-full rounded-lg border bg-background px-2 text-sm";

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="grid gap-1.5 text-sm">
      {label}
      {children}
    </label>
  );
}
