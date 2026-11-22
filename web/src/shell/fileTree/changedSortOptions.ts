// The orders a file list can sort by, named and drawn the same in every Files view.

import {
  ArrowDownAZIcon,
  ArrowDownWideNarrowIcon,
  FileClockIcon,
  FileTypeIcon,
  type LucideIcon,
} from "lucide-react";

import type { ChangedSort } from "@/lib/changedSort";

export const CHANGED_SORT_OPTIONS: readonly {
  value: ChangedSort;
  label: string;
  Icon: LucideIcon;
}[] = [
  { value: "alpha", label: "Filename", Icon: ArrowDownAZIcon },
  { value: "recent", label: "Last edited", Icon: FileClockIcon },
  { value: "size", label: "Size", Icon: ArrowDownWideNarrowIcon },
  { value: "type", label: "Type", Icon: FileTypeIcon },
];
